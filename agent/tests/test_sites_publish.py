"""A site goes live rendered for live (A3p-6 batch 2 Task 2, fix round 0; plan 2026-10-06-agent-a3p-6-2-facts-photos-publish
Q11, spec T11-T14): what `confirm_proposal` publishes for an industry site is not its preview's files but the same document
rendered again for a live site -- no "Replace with your photo", no stock "recent work" and no stock office strip, a canonical
link on every page and the structured data's `url` now that the site has an address -- with the preview's screenshots; the
same preview published twice is still one publish; a rollback brings back the live files; an assistant's own bundle is
published file for file, as before."""

from __future__ import annotations

import json
import logging
import re

import pytest
from fastmcp.exceptions import ToolError

from agent import bundles, config, hosted, landing
from agent.sites import build
from agent.tests.conftest import call_tool, data, tool_text
from agent.tests.test_landing import fake  # noqa: F401 (fixture)
from agent.tests.test_render_check import DESKTOP, PHONE, TABLET
from agent.tests.test_site_upload import _get, _publish, _serving, _upload
from agent.tests.test_sites_build import (  # noqa: F401 (fixtures)
    _biz,
    _build,
    _build_own,
    _count,
    _own,
    dental_library,
    library,
    renderer,
)
from agent.tests.test_sites_render import LABEL, Doc
from agent.tests.test_tools_hosting import HOSTNAME, URL, _checked, _hosted_biz, _mailbox

FAILED = "Admiry couldn't publish your site just now. Try again in a few minutes."  # Task 5's wording


def _page(conn, biz: str) -> dict:
    return conn.execute(
        "SELECT id, version, html, status, sections, check_json, checked_at FROM page WHERE business_id = %s", (biz,)
    ).fetchone()


def _live(conn, biz: str) -> dict[str, str]:
    """The files (path -> SHA-256) of the live version."""
    page = _page(conn, biz)
    return bundles.version_files(conn, page["id"], page["version"])


def _text(conn, sha: str) -> str:
    return bytes(conn.execute("SELECT bytes FROM site_blob WHERE sha256 = %s", (sha,)).fetchone()["bytes"]).decode()


def _pages(conn, files: dict[str, str]) -> dict[str, str]:
    return {path: _text(conn, sha) for path, sha in files.items() if path.endswith(".html")}


def _structured(page: str) -> dict:
    return json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.DOTALL).group(1))


def _logo(page: str) -> dict:
    return next(a for a in Doc(page).all("img") if "brand-logo" in a.get("class", "").split())


# ---------------------------------------------------------------- the roofer


def test_a_site_goes_live_rendered_for_live_and_not_as_its_preview(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    renderer,  # noqa: F811
    fake,  # noqa: F811
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    _own(conn, biz, "logo")
    _own(conn, biz, "key")
    ready = _build_own(conn, biz)
    preview_files = bundles.preview_files(conn, ready.token)
    looked_at = _pages(conn, preview_files)
    # the preview: stock photos labelled, stock shown as "recent work", no address yet to be canonical for
    assert LABEL in looked_at["index.html"] and 'data-section="work"' in looked_at["index.html"]
    assert "canonical" not in looked_at["index.html"] and "url" not in _structured(looked_at["index.html"])

    _publish(client, conn, monkeypatch, key, ready.token, "live")
    live_files, page = _live(conn, biz), _page(conn, biz)
    pages = _pages(conn, live_files)

    assert page["status"] == "published" and page["sections"]["kind"] == "site"
    assert sorted(pages) == sorted(looked_at)  # the same pages, 404 included
    for path, text in pages.items():
        assert LABEL not in text, path  # the label is for looking; a live site never carries it (T13)
        assert 'data-section="work"' not in text, path  # nor stock as the business's "recent work" (T11, T14)
        assert _structured(text)["url"] == f"https://{HOSTNAME}/", path
        canonical = re.findall(r'<link rel="canonical" href="([^"]+)">', text)
        want = [] if path == "404.html" else [f"https://{HOSTNAME}/{path.removesuffix('index.html')}"]
        assert canonical == want, path
    assert page["html"] == pages["index.html"]  # `page.html` is the live home page, not the preview's

    # the owner's logo and key photo are as they were on the preview: the same files under the same names
    owner = {p for p in live_files if p.endswith((".png", ".jpg"))}
    assert len(owner) == 2 and all(live_files[p] == preview_files[p] for p in owner)
    assert _logo(pages["index.html"]) == _logo(looked_at["index.html"])
    hero = next(f for f in Doc(pages["index.html"]).figures if f["attrs"].get("data-slot") == "hero")
    seen = next(f for f in Doc(looked_at["index.html"]).figures if f["attrs"].get("data-slot") == "hero")
    assert hero["attrs"]["data-source"] == "owner" and hero["imgs"] == seen["imgs"]

    # the version's files differ from the preview's where they must -- the pages -- and share the rest by hash
    assert set(live_files) <= set(preview_files)
    assert {p for p in live_files if live_files[p] != preview_files[p]} == set(pages)
    dropped = set(preview_files) - set(live_files)
    assert dropped and all(p.startswith("assets/img/") and p.endswith(".webp") for p in dropped)  # the stock "work"
    stock = {p for p in live_files if p.endswith(".webp")}
    assert stock and all(live_files[p] == preview_files[p] for p in stock)  # the stock photos shown, shared with it
    assert {p for p in live_files if p.startswith(("assets/site-", "assets/fonts/"))} <= set(preview_files)

    # what is served is what was kept; the preview's screenshots went with it; the 14-rule check ran
    served = _get(client, HOSTNAME)
    assert served.status_code == 200 and bundles.sha256(served.content) == live_files["index.html"]
    assert bundles.shot(conn, page["id"], page["version"], "phone") == PHONE
    assert bundles.shot(conn, page["id"], page["version"], "tablet") == TABLET
    assert bundles.shot(conn, page["id"], page["version"], "desktop") == DESKTOP
    assert page["check_json"] is not None and page["checked_at"] is not None
    assert len(renderer.asked) == 1  # the preview's render check was the only one: publishing opens no browser


def test_the_live_site_carries_the_kits_script_tag_as_it_is_now_and_keeps_its_forms(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    fake,  # noqa: F811
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    _own(conn, biz, "logo")
    _own(conn, biz, "key")
    ready = _build_own(conn, biz)
    page_id = landing.ensure_page_for_kit(conn, biz)["id"]
    conn.execute(  # the owner's Google tag came after the preview: the live site carries the kit as it is now
        "INSERT INTO conversion_action (business_id, kind, resource_name, label) VALUES (%s, 'tag', 'customers/1/x', 'AW-777') "
        "ON CONFLICT (business_id, kind) DO UPDATE SET label = EXCLUDED.label",
        (biz,),
    )
    _publish(client, conn, monkeypatch, key, ready.token, "forms")

    assert 'data-gads="AW-777"' not in _text(conn, bundles.preview_files(conn, ready.token)["index.html"])
    pages = _pages(conn, _live(conn, biz))
    for path, text in pages.items():
        assert text.count(f'data-page="{page_id}"') == 1 and 'data-gads="AW-777"' in text, path  # the script tag
    for path in ("contact/index.html", "services/roof-replacement/index.html"):
        assert (
            f'action="http://testserver/t/form/{page_id}"' in pages[path]
            and 'data-admiry-intent="quote"' in pages[path]
        )


# ---------------------------------------------------------------- the dentist


def test_a_dental_site_goes_live_with_its_own_office_photo_alone_and_no_label(
    client,
    conn,
    monkeypatch,
    dental_library,  # noqa: F811
    fake,  # noqa: F811
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    _own(conn, biz, "logo")
    _own(conn, biz, "key")
    ready = _build_own(conn, biz, sample="dentist")
    preview_files = bundles.preview_files(conn, ready.token)
    looked_at = Doc(_pages(conn, preview_files)["index.html"])
    strip = next(f for f in looked_at.figures if f["attrs"].get("data-slot") == "office-2")
    assert LABEL in strip["text"] and strip["attrs"]["data-source"] == "stock"  # the preview's second office photo

    _publish(client, conn, monkeypatch, key, ready.token, "dental")
    live_files = _live(conn, biz)
    pages = _pages(conn, live_files)
    for path, text in pages.items():
        assert LABEL not in text, path
        assert "canonical" in text or path == "404.html", path
        assert _structured(text)["url"] == f"https://{HOSTNAME}/", path
    home = Doc(pages["index.html"])
    office_strip = [f for f in home.figures if f["attrs"].get("data-slot", "").startswith("office-")]
    assert [f["attrs"]["data-slot"] for f in office_strip] == [
        "office-1"
    ]  # only the owner's own, no stock office strip
    assert office_strip[0]["attrs"]["data-source"] == "owner"
    assert not [
        f for f in home.figures if f["attrs"].get("data-source") == "stock" and "office" in f["attrs"]["data-slot"]
    ]
    for path in ("index.html", "contact/index.html", "services/invisalign/index.html"):  # the appointment form stays
        assert 'data-admiry-intent="appointment"' in pages[path] and 'name="patient"' in pages[path], path
    assert {p for p in live_files if p.endswith((".png", ".jpg"))} <= set(preview_files)


# ---------------------------------------------------------------- the same preview twice, a rollback, a fault


def test_publishing_the_same_site_again_is_still_one_publish(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    fake,  # noqa: F811
):
    """H40/H41: what is live decides, not the preview's own bytes -- the live files are no longer the preview's."""
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    _own(conn, biz, "logo")
    _own(conn, biz, "key")
    ready = _build_own(conn, biz)
    _publish(client, conn, monkeypatch, key, ready.token, "once")

    def ask(token: str, n: str) -> dict:
        return data(call_tool(client, "publish_page", {"idempotency_key": f"again-{n}", "token": token}, key=key))

    assert ask(ready.token, "1") == {"status": "already_published"}
    assert _count(conn, "page_version") == 1 and _count(conn, "proposal") == 1
    again = _build_own(conn, biz, checks="static")  # the same photos, built again: the same live site
    assert ask(again.token, "2") == {"status": "already_published"}
    _own(conn, biz, "place")  # a photo more: the live site would change, so it is a publish
    changed = _build_own(conn, biz, checks="static")
    assert ask(changed.token, "3")["status"] == "proposed"
    assert _count(conn, "page_version") == 1  # proposed, not published


def test_a_site_taken_down_is_put_back_with_a_token(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    fake,  # noqa: F811
):
    """Final review I1: a rebuild draws exactly the files of the version the owner took down, but nothing is live (the
    host answers 410) -- so the token proposes a publish, not "already published", and the site comes back."""
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    _own(conn, biz, "logo")
    _own(conn, biz, "key")
    _publish(client, conn, monkeypatch, key, _build_own(conn, biz).token, "up")
    down = data(call_tool(client, "unpublish_page", {"idempotency_key": "down"}, key=key))
    res = call_tool(
        client, "confirm_proposal", {"proposal_id": down["proposal_id"], "idempotency_key": "cb-down"}, key=key
    )
    assert not res["isError"], tool_text(res)
    assert _page(conn, biz)["status"] == "unpublished" and _get(client, HOSTNAME).status_code == 410

    again = _build_own(conn, biz, checks="static")  # the same photos: the same live files as the version taken down
    _publish(client, conn, monkeypatch, key, again.token, "back")  # proposed (not "already published"), confirmed
    assert _page(conn, biz)["status"] == "published" and _page(conn, biz)["version"] == 2
    assert _get(client, HOSTNAME).status_code == 200


def test_an_earlier_version_of_a_site_comes_back_as_it_was_published(
    client,
    conn,
    monkeypatch,
    library,  # noqa: F811
    fake,  # noqa: F811
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    _own(conn, biz, "logo")
    _own(conn, biz, "key")
    _publish(client, conn, monkeypatch, key, _build_own(conn, biz).token, "v1")
    first = _live(conn, biz)
    _own(conn, biz, "place")
    _publish(client, conn, monkeypatch, key, _build_own(conn, biz, checks="static").token, "v2")
    second = _live(conn, biz)
    assert second != first and _page(conn, biz)["version"] == 2

    prop = data(call_tool(client, "publish_page", {"idempotency_key": "pb-back", "version": 1}, key=key))
    assert prop["status"] == "proposed"
    _checked(monkeypatch, conn, URL)
    res = call_tool(
        client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "cb-back"}, key=key
    )
    assert not res["isError"], tool_text(res)
    assert _page(conn, biz)["version"] == 3 and _live(conn, biz) == first  # the files of version 1, not drawn again
    assert LABEL not in _text(conn, _live(conn, biz)["index.html"])


def test_a_fault_in_the_live_render_is_admirys_and_publishes_nothing(
    client,
    conn,
    monkeypatch,
    caplog,
    library,  # noqa: F811
    fake,  # noqa: F811
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    _own(conn, biz, "logo")
    _own(conn, biz, "key")
    ready = _build_own(conn, biz)
    prop = data(call_tool(client, "publish_page", {"idempotency_key": "pb-fault", "token": ready.token}, key=key))
    assert prop["status"] == "proposed"
    real = build.render.bundle

    def broken(*args, **kwargs):
        out = real(*args, **kwargs)
        if kwargs.get("preview") is False:  # only the live drawing: the preview was fine
            out.files["about/index.html"] = b"<!doctype html><html><body><p>No script tag here.</p></body></html>"
        return out

    monkeypatch.setattr(build.render, "bundle", broken)
    with caplog.at_level(logging.ERROR, logger="agent.sites.build"):
        res = call_tool(
            client, "confirm_proposal", {"proposal_id": prop["proposal_id"], "idempotency_key": "cb-fault"}, key=key
        )
    assert res["isError"] and tool_text(res) == FAILED == build.PUBLISH_FAILED
    errors = [r.getMessage() for r in caplog.records if r.name == "agent.sites.build" and r.levelno == logging.ERROR]
    assert len(errors) == 1 and "tracking" in errors[0] and "about/index.html" in errors[0]  # the problems, for the log
    assert _count(conn, "page_version") == 0 and _page(conn, biz)["status"] != "published"
    assert conn.execute("SELECT status FROM proposal WHERE id = %s", (prop["proposal_id"],)).fetchone()["status"] == (
        "proposed"
    )  # the owner's yes is still there to give again


def test_a_document_that_is_gone_or_another_pages_is_never_drawn_for_live(conn, monkeypatch, library):  # noqa: F811
    mine, other = _biz(conn, monkeypatch, "biz_mine"), _biz(conn, monkeypatch, "biz_other")
    page_id = landing.ensure_page_for_kit(conn, mine)["id"]
    theirs = hosted.preview(conn, _build(conn, other).token)["sections"]["site_spec_id"]
    gone, none = {"kind": "site", "site_spec_id": "ss_gone"}, {"kind": "site", "site_spec_id": None}
    for sections in (gone, none, {"kind": "site", "site_spec_id": theirs}):
        with pytest.raises(ToolError) as e:  # in the owner's words, nothing drawn from a document that is not hers
            build.live(conn, config.load(), mine, page_id, sections, hostname=HOSTNAME)
        assert str(e.value) == FAILED, sections


# ---------------------------------------------------------------- what is not a site is untouched


def test_an_assistants_own_bundle_is_published_file_for_file(
    client,
    conn,
    monkeypatch,
    fake,  # noqa: F811
):
    key, biz = _hosted_biz(client, conn, monkeypatch, fake)
    _mailbox(monkeypatch)
    _serving(client)
    ready = _upload(client, key)
    sent = bundles.preview_files(conn, ready["token"])
    _publish(client, conn, monkeypatch, key, ready["token"], "own")
    assert _live(conn, biz) == sent  # the very files, by name and by hash
    page = _page(conn, biz)
    assert page["html"] == _text(conn, sent["index.html"]) and page["sections"] is None
