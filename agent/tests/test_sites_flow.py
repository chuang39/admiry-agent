"""An industry site the whole way, through the tools and the upload link (A3p-6 batch 2 Task 5; plan
2026-10-06-agent-a3p-6-2-facts-photos-publish Q13, Q14 and its Review Focus "The gate, both ways and the rebuild case"):
the first build's e-mail -- sent once, with the preview, the hosting minimum, the photo checklist and the phone link --,
the whole walk of a roofer from `build_site` to a live site (the look, the name, the build, a publish refused until the
logo and the key photo are in, the photos through the link, the preview rebuilt with them, the owner's yes), the rebuild
that failed and the build that heals it, a republish whose live drawing faults, the wordmark standing for a logo, and a
dental practice's appointment form making a lead.

The roofer is `test_tools_sites._roofer` with a plan (publishing needs one) and an owner's e-mail; the dentist is the
dental walk of `test_tools_hosting._hosted_biz`. The model is a FakeGenerator answering the sample's words fitted to the
business, the renderer and Google's speed test the stand-ins of test_sites_build."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from agent import bundles, business, config, hosted, hosting_minimum, jobs, landing, mail
from agent.sites import build, intake, photos
from agent.surface import gate, registry
from agent.tests.conftest import call_tool, data, tool_text
from agent.tests.test_ads_run import add_plan
from agent.tests.test_landing import fake  # noqa: F401 (fixture)
from agent.tests.test_render_check import BROKEN
from agent.tests.test_site_upload import _get, _serving
from agent.tests.test_sites_build import dental_library, renderer, speed  # noqa: F401 (fixtures)
from agent.tests.test_sites_render import library, logo_png  # noqa: F401 (fixture)
from agent.tests.test_tools_hosting import HOSTNAME, URL, _checked, _hosted_biz, _kit_ready_sections, _mailbox
from agent.tests.test_tools_sites import (
    BOTH_MISSING,
    DENTAL_WORDS,
    NO_PHOTOS_YET,
    OAKLINE,
    ROOFER,
    _call,
    _clean,
    _draft,
    _env,
    _fitted,
    _named,
    _roofer,
    model,  # noqa: F401 (fixture)
)
from agent.tests.test_uploads_site import SAVED_LATER, SEE_YOUR_SITE, _jpg, _post, _rows

OWNER = "owner@example.com"
SUBJECT = "Your site is ready to look over"
HEADING = "Photos make your site yours. Upload these, most important first:"
PUBLISH_LINE = "Publish it from your Admiry page or through your assistant once the logo and the photo are in."


@pytest.fixture(autouse=True)
def _sites_on(monkeypatch):
    """AGENT_SITES on (Q22): conftest's environment has it off, as every test written before batch 2 expects."""
    monkeypatch.setenv("AGENT_SITES", "1")


# ---------------------------------------------------------------- the roofer, with an owner to write to


def _with_owner(conn, biz: str = ROOFER, email: str | None = OWNER) -> None:
    conn.execute("UPDATE business SET owner_email = %s, claim_state = 'claimed' WHERE id = %s", (email, biz))


def _owned_roofer(conn, monkeypatch) -> str:
    """The roofer with the name the owner gave (nothing is missing) and an address the owner can be written to."""
    key = _named(conn, monkeypatch)
    _with_owner(conn)
    return key


def _mails(sent: list) -> list[dict]:
    return [json.loads(request.content) for request in sent]


# ---------------------------------------------------------------- the first build's e-mail (Q13)


def test_the_first_build_writes_to_the_owner_once_with_the_preview_the_checklist_and_the_upload_link(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811 - the fixture shadows the imported name
    model,  # noqa: F811
):
    key = _owned_roofer(conn, monkeypatch)
    sent = _mailbox(monkeypatch)

    built = _env(_call(client, key, style="clean"))

    (mailed,) = _mails(sent)
    view, text = built["data"], mailed["text"]
    assert mailed["to"] == [OWNER] and mailed["subject"] == SUBJECT
    assert view["preview_url"] in text and view["upload_url"] in text
    assert text.startswith(f"{OAKLINE}'s site is ready to look over: {view['preview_url']}")
    assert hosting_minimum.MINIMUM in text and NO_PHOTOS_YET in text and PUBLISH_LINE in text
    assert HEADING in text
    for kind in photos.KINDS["home_services"]:
        assert f"[ ] {kind['label']}: 0 of {kind['need']}" in text, kind["id"]
    assert text.index(HEADING) < text.index(view["upload_url"]) < text.index(NO_PHOTOS_YET) < text.index(PUBLISH_LINE)
    assert _clean(text) and _clean(mailed["subject"])
    assert _draft(conn)["mailed_ready"] is True


def test_a_second_build_writes_nothing_more(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,  # noqa: F811
):
    key = _owned_roofer(conn, monkeypatch)
    sent = _mailbox(monkeypatch)
    _env(_call(client, key, style="clean"))
    assert len(sent) == 1

    _env(_call(client, key, style="bold"))  # a new look
    _env(_call(client, key, facts={"warranty": "10-year workmanship warranty"}))  # a new fact: the words again
    _env(_call(client, key))  # the same again

    assert len(sent) == 1 and _draft(conn)["mailed_ready"] is True


def test_the_checklist_in_the_e_mail_marks_what_is_done(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,  # noqa: F811
):
    """The owner accepts the wordmark in the very call that builds: the logo reads done in the e-mail, the rest not."""
    key = _owned_roofer(conn, monkeypatch)
    sent = _mailbox(monkeypatch)

    _env(_call(client, key, style="solid", wordmark=True))

    (mailed,) = _mails(sent)
    assert "[x] Your logo: 0 of 1" in mailed["text"]
    assert "[ ] One photo of your own work, your team or your truck: 0 of 1" in mailed["text"]


def test_with_no_address_to_write_to_nothing_is_sent_nobody_is_told_and_the_next_build_tries_again(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,  # noqa: F811
):
    key = _named(conn, monkeypatch)  # no owner address on file
    sent = _mailbox(monkeypatch)

    env = _env(_call(client, key, style="clean"))

    assert sent == [] and env["data"]["status"] == "preview" and _draft(conn)["mailed_ready"] is False
    _with_owner(conn)  # the address arrives
    _env(_call(client, key, style="bold"))
    assert len(sent) == 1 and _draft(conn)["mailed_ready"] is True
    _env(_call(client, key, style="clean"))
    assert len(sent) == 1


def test_a_server_that_cannot_send_mail_builds_all_the_same_and_tries_again_later(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,  # noqa: F811
):
    key = _owned_roofer(conn, monkeypatch)  # RESEND_API_KEY is not set: nothing can be sent

    env = _env(_call(client, key, style="clean"))

    assert env["data"]["status"] == "preview" and _draft(conn)["mailed_ready"] is False
    sent = _mailbox(monkeypatch)  # the key is set
    _env(_call(client, key, style="bold"))
    assert len(sent) == 1 and _draft(conn)["mailed_ready"] is True


def test_a_refused_letter_is_no_error_and_no_flag_and_the_next_build_sends_it(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,  # noqa: F811
):
    key = _owned_roofer(conn, monkeypatch)
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")
    tried: list = []

    def refuse(request: httpx.Request) -> httpx.Response:
        tried.append(request)
        return httpx.Response(500, json={"message": "down"})

    monkeypatch.setattr(mail, "client", lambda: httpx.Client(transport=httpx.MockTransport(refuse)))

    env = _env(_call(client, key, style="clean"))

    assert len(tried) == 1 and env["data"]["status"] == "preview" and _draft(conn)["mailed_ready"] is False
    sent = _mailbox(monkeypatch)
    _env(_call(client, key, style="bold"))
    assert len(sent) == 1 and _draft(conn)["mailed_ready"] is True


def test_a_question_a_look_asked_and_a_refusal_write_nothing(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,  # noqa: F811
):
    """Only a site that was built is news: the look asked, the name asked and a look the family has not are not."""
    key = _roofer(conn, monkeypatch)  # the name is still to give
    _with_owner(conn)
    sent = _mailbox(monkeypatch)

    assert _env(_call(client, key))["next"]["question"]["id"] == "style"  # the look asked first
    assert _env(_call(client, key, style="solid"))["next"]["question"]["id"] == "name"  # then the name
    assert _call(client, key, style="calm")["isError"]  # a look of the other family

    assert sent == [] and _draft(conn)["mailed_ready"] is False
    _env(_call(client, key, facts={"name": OAKLINE}))  # the look is kept: the name answers the last question
    assert len(sent) == 1


def test_a_build_through_the_pages_own_command_writes_the_same_letter(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,  # noqa: F811
):
    """`create_page` hands a family business to the site (Task 5): once the look is picked and nothing is missing, the
    call that builds is `create_page`'s -- and its first build writes to the owner as `build_site`'s does."""
    key = _roofer(conn, monkeypatch)  # the name is still to give
    _with_owner(conn)
    sent = _mailbox(monkeypatch)
    asked = _env(_call(client, key, style="clean"))
    assert asked["next"]["question"]["id"] == "name" and sent == []  # the look is kept, the name asked
    _version, knowledge = business.latest_knowledge(conn, ROOFER)
    business.add_knowledge(conn, ROOFER, {**knowledge, "name": OAKLINE, "name_source": "owner"}, source="owner")

    env = _env(_call(client, key, "create_page"))

    assert env["data"]["status"] == "preview" and env["data"]["style"] == "clean"
    (mailed,) = _mails(sent)
    assert mailed["subject"] == SUBJECT and env["data"]["preview_url"] in mailed["text"]
    assert _draft(conn)["mailed_ready"] is True
    # the call's row is create_page's and says what it was: the site's, and the model wrote its words (a draft of the day)
    row = conn.execute("SELECT args_redacted FROM agent_action WHERE tool = 'create_page'").fetchone()
    assert (row["args_redacted"]["site"], row["args_redacted"]["model"]) == ("true", "true")
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'build_site'").fetchone()["n"] == 1


# ---------------------------------------------------------------- the whole way, through the tools and the link

REBUILD = "Your photos are in; build the site once more, then publish."
PUBLISH_FAILED = "Admiry couldn't publish your site just now. Try again in a few minutes."
LOGO = ("logo.png", logo_png(), "image/png")


def _planned_roofer(conn, monkeypatch) -> str:
    """The roofer whose name is still to give, with an owner to write to and a plan on file: publishing needs one, and
    nothing else here reads it."""
    key = _roofer(conn, monkeypatch)
    _with_owner(conn)
    secs = _kit_ready_sections()
    secs["consultation"]["business"] = {
        "category": "home_services",
        "service": "Roof replacement",
        "services": ["Roof replacement", "Roof repair"],
        "location": "9 Elm Ct, Charlotte, NC 28203, USA",
    }
    add_plan(conn, secs, biz=ROOFER)
    return key


def _host(conn) -> str:
    """The address the roofer's site goes live at."""
    return hosted.hostname_for(conn, ROOFER, OAKLINE, config.load().pages_domain)


def _link_token(upload_url: str) -> str:
    return upload_url.rsplit("/", 1)[1]


def _preview_token(client, key: str) -> str:
    """The token of the business's site preview as `get_site` shows it now."""
    return _env(_call(client, key, "get_site"))["data"]["preview"]["token"]


def _document(conn, token: str) -> dict:
    """The document the preview with this token was built from."""
    spec_id = hosted.preview(conn, token)["sections"]["site_spec_id"]
    return conn.execute("SELECT data FROM site_spec WHERE id = %s", (spec_id,)).fetchone()["data"]


def _publish(client, conn, monkeypatch, key: str, token: str, idem: str, *, url: str) -> dict:
    """publish_page(token) -> the owner's yes (confirm_proposal): what confirm answers."""
    prop = data(call_tool(client, "publish_page", {"idempotency_key": f"pb-{idem}", "token": token}, key=key))
    assert prop["status"] == "proposed", prop
    _checked(monkeypatch, conn, url)
    res = call_tool(
        client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": f"cb-{idem}"}, key=key
    )
    assert not res["isError"], tool_text(res)
    return data(res)


def _page_row(conn, biz: str = ROOFER) -> dict:
    return conn.execute("SELECT * FROM page WHERE business_id = %s", (biz,)).fetchone()


def _live_roofer(client, conn, monkeypatch) -> tuple[str, str]:
    """The roofer's site built, its logo and key photo sent through the link, and published: (key, upload token)."""
    key = _planned_roofer(conn, monkeypatch)
    _mailbox(monkeypatch)
    _serving(client)
    view = _env(_call(client, key, style="solid", facts={"name": OAKLINE}))["data"]
    token = _link_token(view["upload_url"])
    assert _post(client, token, "logo", LOGO).status_code == 200
    assert _post(client, token, "key", _jpg("truck.jpg", 90)).status_code == 200
    _publish(client, conn, monkeypatch, key, _preview_token(client, key), "live", url=f"https://{_host(conn)}/")
    return key, token


def test_a_roofer_goes_from_build_site_to_a_live_site_through_the_tools_and_the_upload_link(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811 - the fixture shadows the imported name
    renderer,  # noqa: F811
    speed,  # noqa: F811
    model,  # noqa: F811
):
    """The Review Focus gate case end to end: the name asked, the look asked, the site built; a publish refused until the
    logo and one photo of the business's own are in; the logo and the key photo through the phone link, two batches,
    each building the preview again with no model and no render check; the new preview's document names both; the
    publish proposed on the new preview's token and published on the owner's yes."""
    key = _planned_roofer(conn, monkeypatch)
    sent = _mailbox(monkeypatch)
    _serving(client)
    host = _host(conn)

    asked = _env(
        _call(client, key)
    )  # the look, the name, the build: the look first (the profile's name is the model's)
    assert asked["next"]["question"]["id"] == "style"
    picked = _env(_call(client, key, style="solid"))
    assert picked["next"]["question"]["id"] == "name" and model.site_calls == []
    built = _env(_call(client, key, facts={"name": OAKLINE}))
    view = built["data"]
    assert view["status"] == "preview" and view["key_photos"]["ok"] is False and len(model.site_calls) == 1
    assert len(renderer.asked) == 1 and len(speed.asked) == 1  # the full build opened the site and timed it once
    first = view["token"]

    held = call_tool(client, "publish_page", {"idempotency_key": "pb-held", "token": first}, key=key)
    assert held["isError"] and tool_text(held) == BOTH_MISSING
    assert conn.execute("SELECT count(*) AS n FROM proposal").fetchone()["n"] == 0  # nothing proposed

    link = _link_token(view["upload_url"])
    logo = _post(client, link, "logo", LOGO)
    assert logo.status_code == 200 and SEE_YOUR_SITE.search(logo.text)
    still = _env(_call(client, key, "get_site"))["data"]
    key_only = "Your site needs one photo of your own work before it goes live."  # the logo is in; the key photo is not
    assert still["key_photos"]["ok"] is False and still["key_photos"]["sentence"] == key_only
    alone = call_tool(
        client, "publish_page", {"idempotency_key": "pb-logo", "token": still["preview"]["token"]}, key=key
    )
    assert alone["isError"] and tool_text(alone) == key_only
    key_photo = _post(client, link, "key", _jpg("truck.jpg", 90))
    rebuilt = SEE_YOUR_SITE.search(key_photo.text).group(1)
    # a photo change builds statically: the words are the draft's, the site is not opened, nor timed
    assert (len(model.site_calls), len(renderer.asked), len(speed.asked)) == (1, 1, 1)

    site = _env(_call(client, key, "get_site"))["data"]
    assert site["status"] == "preview" and site["preview"]["url"] == rebuilt and site["key_photos"]["ok"] is True
    assert [(i["id"], i["have"], i["done"]) for i in site["checklist"]][:2] == [("logo", 1, True), ("key", 1, True)]
    new = site["preview"]["token"]
    assert new != first and hosted.preview(conn, first) is None  # the earlier preview ended with the rebuild
    logo_id, key_id = (_rows(conn, _page_row(conn)["id"], kind)[0]["id"] for kind in ("logo", "key"))
    images = _document(conn, new)["images"]
    assert (images["logo"]["source"], images["logo"]["id"]) == ("owner", logo_id)
    assert (images["hero"]["source"], images["hero"]["id"]) == ("owner", key_id)
    assert build.publish_gate(conn, hosted.preview(conn, new)) is None

    published = _publish(client, conn, monkeypatch, key, new, "walk", url=f"https://{host}/")
    page = _page_row(conn)
    assert published["page"]["hostname"] == host
    assert (page["status"], page["source"], page["sections"]["kind"], page["version"]) == (
        "published",
        "admiry",
        "site",
        1,
    )
    live = _get(client, host)
    assert live.status_code == 200 and OAKLINE in live.text and 'class="brand-logo"' in live.text
    said = _env(_call(client, key, "get_site"))
    assert said["say"] == f"Your site is live at https://{host}/." and said["data"]["status"] == "published"
    subjects = [json.loads(r.content)["subject"] for r in sent]
    assert subjects[0] == SUBJECT and subjects.count(SUBJECT) == 1  # the one letter of the first build


def test_an_assistant_walks_the_roofer_to_a_live_site_through_the_public_tools(
    client,
    conn,
    monkeypatch,
    strict_surface,
    library,  # noqa: F811
    renderer,  # noqa: F811
    speed,  # noqa: F811
    model,  # noqa: F811
):
    """A3p-6 batch 2 Task 6 (plan Q15, Q16): the same walk as an assistant makes it, the commands hidden (the gate as
    production runs it): the looks shown by `get_site_templates`, `write` asking the name, `build_site` carrying the
    answers (`facts`, then `style`), the site view saying where it stands, the photos through the owner's link, and the
    owner's two-call yes through `update_landing_page`. Every action in a `next` is a public tool with the arguments that
    pick it, and the commands behind them are not callable."""
    key = _planned_roofer(conn, monkeypatch)
    _mailbox(monkeypatch)
    _serving(client)
    host = _host(conn)
    calls = iter(range(1, 100))

    def page(**args) -> dict:
        return _call(client, key, "update_landing_page", idempotency_key=f"gw-{next(calls)}", **args)

    assert _call(client, key, "build_site")["isError"] and _call(client, key, "get_site")["isError"]  # not tools
    looks = _env(_call(client, key, "get_site_templates"))["data"]["families"]
    assert [(f["id"], [look["id"] for look in f["styles"]]) for f in looks] == [
        ("home_services", ["solid", "clean", "bold"]),
        ("dental", ["calm", "warm", "boutique"]),
    ]
    asked = _env(page(action="write"))  # the page step of a home-services business: the look is the first question
    question = asked["next"]["question"]
    assert question["id"] == "style" and [o["id"] for o in question["options"]] == ["solid", "clean", "bold"]
    assert model.site_calls == [] and asked["data"]["site"]["missing"] == ["name"]
    picked = _env(page(action="build_site", style="solid"))  # then the fact Admiry lacks
    assert picked["next"]["question"]["id"] == "name" and model.site_calls == []
    built = _env(page(action="build_site", facts={"name": OAKLINE}))
    view = built["data"]
    assert view["status"] == "preview" and view["key_photos"]["ok"] is False and len(model.site_calls) == 1
    assert built["next"] == {"kind": "link", "link": {"url": view["upload_url"], "label": "Add your logo and photos"}}
    seen = _env(_call(client, key, "get_landing_page", view="site"))["data"]
    assert seen["status"] == "preview" and seen["preview"]["token"] == view["token"]
    assert seen["upload_url"] == view["upload_url"] and seen["key_photos"]["sentence"] == BOTH_MISSING
    # what the page tool's description tells an assistant the view carries is what it carries, nested keys included
    described = " ".join(next(m for m in registry.SURFACE if m.name == "get_landing_page").tool.description.split())
    assert set(seen["preview"]) == {"url", "token", "expires_at", "render"}
    assert set(seen["preview"]["render"]) == {"loads", "partial"}
    for found in ("url", "token", "expires_at", "render", "loads", "partial"):
        assert f"`{found}`" in described, found

    link = _link_token(view["upload_url"])  # the owner's phone: photos reach Admiry only here
    assert _post(client, link, "logo", LOGO).status_code == 200
    assert _post(client, link, "key", _jpg("truck.jpg", 90)).status_code == 200
    # fix round 1: each upload rebuilt the preview and ended the earlier one, so the token the build gave is dead by now;
    # the view shows the live one, and publishing with it needs no second build (nor a second call of the model)
    dead = page(action="publish", token=view["token"])
    assert dead["isError"] and "This preview has expired or is unknown" in tool_text(dead)
    now = _env(_call(client, key, "get_landing_page", view="site"))["data"]
    token = now["preview"]["token"]
    assert token != view["token"] and now["key_photos"]["ok"] is True and len(model.site_calls) == 1
    proposed = _env(page(action="publish", token=token))
    assert proposed["data"]["status"] == "proposed"
    _checked(monkeypatch, conn, f"https://{host}/")
    done = _env(page(action="publish", token=token, owner_confirmed=True))
    assert done["data"]["page"]["hostname"] == host
    live = _env(_call(client, key, "get_landing_page", view="site"))["data"]
    assert live["status"] == "published" and live["published"]["url"] == f"https://{host}/"
    assert set(live["published"]) == {"url", "hostname", "version", "check"} and live["muse"]
    for found in live["published"]:
        assert f"`{found}`" in described, found
    assert _get(client, host).status_code == 200

    # the owner changes the look of the live site: with the photos in, the build offers to publish it or to change it
    # again, as public tools with the arguments that pick them
    changed = _env(page(action="build_site", style="clean"))
    offered = changed["next"]["actions"]
    assert changed["data"]["style"] == "clean" and changed["data"]["key_photos"]["ok"] is True
    assert [(a["id"], a["label"], a["tool"], a["args"]) for a in offered] == [
        ("publish", "Publish your site", "update_landing_page", {"action": "publish"}),
        ("look", "Change the look", "update_landing_page", {"action": "build_site"}),
    ]
    assert offered[0]["primary"] is True and {a["tool"] for a in offered} <= gate.PUBLIC
    assert len(model.site_calls) == 1  # a new look keeps the words


def test_a_key_photo_whose_rebuild_failed_waits_for_one_more_build_and_then_goes_live(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,  # noqa: F811
    model,  # noqa: F811
):
    """The rebuild case: a key photo uploaded while the static rebuild fails (the template fault stood in) is saved and
    the preview still lacks it -- the gate says so (REBUILD_FIRST) -- until `build_site` builds again, which asks the
    model nothing and gives a preview that publishes."""
    key = _planned_roofer(conn, monkeypatch)
    _mailbox(monkeypatch)
    _serving(client)
    view = _env(_call(client, key, style="solid", facts={"name": OAKLINE}))["data"]
    link = _link_token(view["upload_url"])
    assert _post(client, link, "logo", LOGO).status_code == 200
    real = build.preview

    def failing(*args, **kwargs):
        if kwargs.get("checks") == "static":
            raise build.TemplateFault([bundles.problem("/", "stand_in", "The stand-in refuses the build.", "Nothing.")])
        return real(*args, **kwargs)

    monkeypatch.setattr(build, "preview", failing)
    saved = _post(client, link, "key", _jpg("truck.jpg", 90))
    monkeypatch.setattr(build, "preview", real)

    assert saved.status_code == 200 and SAVED_LATER in saved.text
    assert len(_rows(conn, _page_row(conn)["id"], "key")) == 1  # the photo is kept, whatever the rebuild did
    site = _env(_call(client, key, "get_site"))["data"]
    assert site["key_photos"]["ok"] is True  # the photos are on file ...
    stale = site["preview"]["token"]
    assert "hero" not in _document(conn, stale)["images"]  # ... and the preview the owner has shows no key photo
    res = call_tool(client, "publish_page", {"idempotency_key": "pb-stale", "token": stale}, key=key)
    assert res["isError"] and tool_text(res) == REBUILD == build.REBUILD_FIRST
    assert conn.execute("SELECT count(*) AS n FROM proposal").fetchone()["n"] == 0

    # build_site with nothing to say: the same look, the words as they were, the photos as they are
    again = _env(_call(client, key))
    assert again["data"]["status"] == "preview" and len(model.site_calls) == 1
    assert "hero" in _document(conn, again["data"]["token"])["images"]
    assert again["next"]["kind"] == "actions" and again["next"]["actions"][0]["tool"] == "publish_page"
    _publish(client, conn, monkeypatch, key, again["data"]["token"], "healed", url=f"https://{_host(conn)}/")
    assert _page_row(conn)["status"] == "published"


def test_a_photo_after_a_change_of_the_facts_the_words_read_is_kept_for_the_next_build(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    model,  # noqa: F811
):
    """Final review I5: a photo's rebuild keeps the site's words only for the facts they were written from -- the rule
    build_site keeps them by (`words.fingerprint` against the draft's `words_for`). With the facts as they were, a photo
    builds the preview again; once a fact the model reads has changed on the profile (the hours), a photo is saved and no
    preview is drawn pairing the old words with the new facts: the page says the site shows it the next time it is built,
    and the build that comes next writes the words again."""
    key = _named(conn, monkeypatch)
    view = _env(_call(client, key, style="solid"))["data"]
    link = _link_token(view["upload_url"])
    same = _post(client, link, "logo", LOGO)
    assert same.status_code == 200 and SEE_YOUR_SITE.search(same.text)  # the facts as they were: built again
    shown, previews = _preview_token(client, key), _count_previews(conn)

    _version, knowledge = business.latest_knowledge(conn, ROOFER)
    business.add_knowledge(conn, ROOFER, {**knowledge, "hours": "Mon–Sat 8am–5pm"}, source="owner")
    later = _post(client, link, "key", _jpg("truck.jpg", 90))

    assert later.status_code == 200 and SAVED_LATER in later.text and not SEE_YOUR_SITE.search(later.text)
    assert len(_rows(conn, _page_row(conn)["id"], "key")) == 1  # the photo is kept
    assert _preview_token(client, key) == shown and _count_previews(conn) == previews  # no preview drawn
    assert len(model.site_calls) == 1
    again = _env(_call(client, key))["data"]  # the next build: the words written again, the photo in it
    assert len(model.site_calls) == 2 and "hero" in _document(conn, again["token"])["images"]


def _count_previews(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM page_preview").fetchone()["n"]


def test_a_republish_whose_live_drawing_faults_keeps_the_version_that_is_serving(
    client,
    conn,
    monkeypatch,
    caplog,
    library,  # noqa: F811
    renderer,  # noqa: F811
    model,  # noqa: F811
):
    """A site drawn again for live at the owner's yes is Admiry's template at work: when that drawing faults, the answer
    is the publish failure's sentence -- at the request already, where a republish compares what it would put live with
    what is -- and version 1 keeps serving as it was."""
    key, link = _live_roofer(client, conn, monkeypatch)
    host = _host(conn)
    before = _get(client, host).text
    assert (
        _post(client, link, "place", _jpg("yard.jpg", 60)).status_code == 200
    )  # a photo more: a republish would differ
    new = _preview_token(client, key)
    real = build._render_checked

    def faulty(*args, **kwargs):
        if kwargs.get("preview") is False:  # only the drawing for live
            raise build.TemplateFault(
                [bundles.problem("/", "stand_in", "The stand-in refuses the drawing.", "Nothing.")]
            )
        return real(*args, **kwargs)

    monkeypatch.setattr(build, "_render_checked", faulty)
    with caplog.at_level(logging.ERROR, logger="agent.sites.build"):
        res = call_tool(client, "publish_page", {"idempotency_key": "pb-faulty", "token": new}, key=key)

    assert res["isError"] and tool_text(res) == PUBLISH_FAILED == build.PUBLISH_FAILED and _clean(PUBLISH_FAILED)
    errors = [r for r in caplog.records if r.name == "agent.sites.build" and r.levelno == logging.ERROR]
    assert len(errors) == 1 and "stand-in refuses the drawing" in errors[0].getMessage()
    page = _page_row(conn)
    assert (page["status"], page["version"]) == ("published", 1)
    assert conn.execute("SELECT count(*) AS n FROM page_version").fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM proposal").fetchone()["n"] == 1  # the first publish's, no new one
    assert _get(client, host).text == before  # what was live is live


def test_a_site_published_with_the_wordmark_and_a_key_photo_shows_the_business_name_as_its_logo(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,  # noqa: F811
    model,  # noqa: F811
):
    """T13: with no logo, the owner's yes to using their business name as one -- and the key photo -- is enough; the live
    header shows the name, in every page, and there is no logo image in the site."""
    key = _planned_roofer(conn, monkeypatch)
    _mailbox(monkeypatch)
    _serving(client)
    built = _env(_call(client, key, style="clean", facts={"name": OAKLINE}, wordmark=True))
    view = built["data"]
    assert view["key_photos"]["logo"] is False and view["key_photos"]["wordmark_ok"] is True
    assert view["key_photos"]["sentence"] == "Your site needs one photo of your own work before it goes live."
    held = call_tool(client, "publish_page", {"idempotency_key": "pb-hold", "token": view["token"]}, key=key)
    assert held["isError"] and tool_text(held) == view["key_photos"]["sentence"]

    assert _post(client, _link_token(view["upload_url"]), "key", _jpg("truck.jpg", 90)).status_code == 200
    site = _env(_call(client, key, "get_site"))["data"]
    assert site["key_photos"]["ok"] is True and site["key_photos"]["logo"] is False
    assert [i["done"] for i in site["checklist"]][:2] == [True, True]  # the logo is done by the wordmark
    host = _host(conn)
    _publish(client, conn, monkeypatch, key, site["preview"]["token"], "wordmark", url=f"https://{host}/")

    for path in ("/", "/about/", "/contact/"):
        page = _get(client, host, path).text
        assert f'<a class="brand" href="/">{OAKLINE}</a>' in page and "brand-logo" not in page, path
    assert _page_row(conn)["status"] == "published"


def test_a_dental_site_goes_live_with_its_appointment_form_and_the_patient_answer_makes_a_lead(
    client,
    conn,
    monkeypatch,
    dental_library,  # noqa: F811
    renderer,  # noqa: F811
    fake,  # noqa: F811
):
    """The dental walk: its facts all on file, the look the only question; its logo and an office photo through the
    link; live, its contact page carries the appointment form, and a post to the page's form address with the patient's
    answer makes a lead that keeps it."""
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    fake.sites = [_fitted(*DENTAL_WORDS)]

    asked = _env(_call(client, key))
    assert asked["next"]["question"]["id"] == "style"
    view = _env(_call(client, key, style="calm"))["data"]
    assert view["family"] == "dental" and view["key_photos"]["ok"] is False
    held = call_tool(client, "publish_page", {"idempotency_key": "pb-dental-hold", "token": view["token"]}, key=key)
    assert (
        held["isError"]
        and tool_text(held) == "Your site needs your logo and one photo of your office before it goes live."
    )
    link = _link_token(view["upload_url"])
    assert _post(client, link, "logo", LOGO).status_code == 200
    assert _post(client, link, "key", _jpg("office.jpg", 120)).status_code == 200
    _publish(client, conn, monkeypatch, key, _preview_token(client, key), "dental", url=URL)

    contact = _get(client, HOSTNAME, "/contact/").text
    page_id = landing.current(conn, biz)["id"]
    assert 'data-admiry-intent="appointment"' in contact and 'name="patient"' in contact
    assert f'action="http://testserver/t/form/{page_id}"' in contact
    form = {
        "name": "Ana",
        "phone": "4085550199",
        "patient": "New patient",
        "service": "Invisalign",
        "preferred_time": "Morning",
        "_intent": "appointment",
    }
    answer = client.post(f"/t/form/{page_id}", data=form).json()
    assert answer["lead"] is True and answer["conversion"] == "appointment"
    row = conn.execute("SELECT fields, intent FROM lead WHERE business_id = %s", (biz,)).fetchone()
    assert row["intent"] == "appointment" and row["fields"]["patient"] == "New patient"


def test_a_site_that_is_live_already_is_sent_no_ready_letter(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,  # noqa: F811
    speed,  # noqa: F811
    model,  # noqa: F811
):
    """Task 6 (Task 5's review): a site built before this release is live, and its draft holds neither words nor the
    letter's flag, so its next build reads as the first one. The letter says the site is ready to look over and to
    publish once the photos are in: for a site that is live already it would be wrong -- no letter, and no flag set."""
    key, _link = _live_roofer(client, conn, monkeypatch)
    page = _page_row(conn)
    assert page["status"] == "published" and page["sections"]["kind"] == "site"
    intake.update_draft(conn, page["id"], {"words": None, "words_for": None, "mailed_ready": False})  # built before
    sent = _mailbox(monkeypatch)

    env = _env(_call(client, key, style="bold"))

    assert env["data"]["status"] == "published" and env["data"]["preview"] is not None
    assert sent == [] and _draft(conn)["mailed_ready"] is False


# ---------------------------------------------------------------- the daily look at a live site (Task 5, the jobs)


def test_a_site_admiry_built_is_looked_at_each_day_and_a_problem_is_the_operators_never_the_owners(
    client,
    conn,
    monkeypatch,
    caplog,
    library,  # noqa: F811
    renderer,  # noqa: F811
    model,  # noqa: F811
):
    """A live bundle is opened again by the render check once a day (A3p-3). A site Admiry built is included now; what
    the check finds on it is the template's fault, so the owner is not e-mailed -- the operator's log has the problems --
    while the page keeps them for `get_page` and the day is spent as for any other (an assistant's bundle keeps its
    e-mail: test_render_check)."""
    key, _link = _live_roofer(client, conn, monkeypatch)
    sent = _mailbox(monkeypatch)
    monkeypatch.setattr(landing, "reachable", lambda url, **kwargs: (True, "200"))
    contact = {"file": "/contact/", "rule": "console", "message": "The form's script did not load.", "fix": "Fix it."}
    renderer.problems = [BROKEN, contact]
    host = _host(conn)

    with caplog.at_level(logging.ERROR, logger="agent.jobs"):
        out = jobs.page_check(conn, 0, 1)

    assert (out["rendered"], out["render_mailed"], out["render_errors"]) == (1, 0, 0)
    assert sent == [] and renderer.asked[-1]["url"] == f"https://{host}/"
    (record,) = [r for r in caplog.records if r.name == "agent.jobs" and r.levelno == logging.ERROR]
    assert host in record.getMessage() and BROKEN["message"] in record.getMessage()
    # Task 6: each problem is named by its file and its rule, with its message, so the operator knows where to look
    for found in (BROKEN, contact):
        assert f"{found['file']} ({found['rule']}): {found['message']}" in record.getMessage()
    kept = data(call_tool(client, "get_page", {}, key=key))["page"]["render"]
    assert kept["problems"] == [BROKEN, contact] and kept["at"]
    assert jobs.page_check(conn, 0, 1)["rendered"] == 0  # once a day

    conn.execute("UPDATE page SET rendered_at = now() - interval '2 days' WHERE business_id = %s", (ROOFER,))
    renderer.problems = []
    clear = jobs.page_check(conn, 0, 1)
    assert (clear["rendered"], clear["render_mailed"]) == (1, 0)
    assert data(call_tool(client, "get_page", {}, key=key))["page"]["render"]["problems"] == []
