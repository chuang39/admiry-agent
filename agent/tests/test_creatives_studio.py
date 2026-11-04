"""The creatives studio's pipeline (spec 2026-11-04 §4, rulings R2, R3, R15): a set made from the business's own page
photos and facts, written to ad_creative_set / ad_creative_image, shown as the card, accepted for Google. The
DB is the test DB; the models are a fake MetaClient answering by schema and by colour: a photo's "work" is its red
rectangle, which the fake SAM finds wherever it is. No network."""

from __future__ import annotations

import io
import json
import threading
import time

import pytest
from fastmcp.exceptions import ToolError
from PIL import Image, ImageChops
from psycopg.types.json import Jsonb

from agent import config
from agent.creatives import copy, images, meta, studio

BASE = "http://testserver"
KNOWLEDGE = {
    "name": "Oakline Roofing",
    "trade": "roofing",
    "category": "home_services",
    "category_detail": "roofer",
    "services": [{"name": "Roof repair", "intent_phrases": ["roof repair"], "price_hint": None}],
    "service_area": ["Charlotte"],
    "phone": "(704) 555-0142",
    "offers": [{"id": "o1", "text": "Free roof inspections", "terms": None}],
}
WIDE = (50, 100, 950, 700)  # in a 1000x800 photo: too tall for 1.91:1, too wide for 1:1 -> extended both ways
SMALL = (400, 300, 600, 500)  # fits both plain crops -> no generation


def photo_bytes(size=(1000, 800), work=WIDE) -> bytes:
    img = Image.new("RGB", size, (40, 160, 40))
    if work:
        img.paste((220, 20, 20), work)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def red_mask(img: Image.Image) -> Image.Image:
    r, g, b = img.convert("RGB").split()
    on = ImageChops.multiply(r.point(lambda v: 255 if v > 150 else 0), g.point(lambda v: 255 if v < 90 else 0))
    return ImageChops.multiply(on, b.point(lambda v: 255 if v < 90 else 0))


class FakeMeta:
    def __init__(self, *, privacy=None, sam_empty=False, image_ok=True, edit_delay=0.0, image_check="answer"):
        self.privacy = privacy or {}
        self.sam_empty = sam_empty
        self.image_ok = image_ok
        self.image_check = image_check  # answer | down | ok_with_problems
        self.parts: dict[str, list[list[dict]]] = {}
        self.edit_delay = edit_delay
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []

    def _log(self, model, what):
        with self.lock:
            self.calls.append((model, what))

    def count(self, what):
        return sum(1 for _, w in self.calls if w == what)

    def json(self, model, system, parts, schema):
        self._log(model, schema.__name__)
        with self.lock:
            self.parts.setdefault(schema.__name__, []).append(parts)
        body = json.loads(parts[0]["text"])
        if schema is copy.PhotoRead:
            flags = self.privacy.get(body["photo_id"], [])
            return copy.PhotoRead(
                photo_id=body["photo_id"], subject="new red roof", suitable=True, reason=None, privacy=flags
            )
        if schema is studio.ImageCheck:
            if self.image_check == "down":
                raise meta.MetaError("muse-wonder-1.0 is unavailable (503)")
            if self.image_check == "ok_with_problems":
                return studio.ImageCheck(ok=True, problems=["a faint seam at the left"])
            return studio.ImageCheck(ok=self.image_ok, problems=[] if self.image_ok else ["a visible seam"])
        if schema is copy.AdText:
            facts = {f["text"]: f["id"] for f in body["facts"]}
            name, offer = facts["Name: Oakline Roofing"], facts["Offer: Free roof inspections"]
            return copy.AdText(
                headlines=[
                    copy.Claim(text="Oakline Roofing", fact_ids=[name]),
                    copy.Claim(text="Free Roof Inspections", fact_ids=[offer]),
                    copy.Claim(text="Roofers in Charlotte", fact_ids=[name, facts["Service area: Charlotte"]]),
                    copy.Claim(text="Licensed & Insured", fact_ids=[name]),
                ],
                descriptions=[
                    copy.Claim(text="Oakline Roofing repairs roofs in Charlotte.", fact_ids=[name]),
                    copy.Claim(text="Ask Oakline Roofing for a free roof inspection.", fact_ids=[name, offer]),
                ],
                callouts=[copy.Claim(text="Free Inspections", fact_ids=[offer])],
            )
        if schema is copy.Check:
            return copy.Check(
                verdicts=[
                    copy.Verdict(
                        text=c["text"],
                        grounded="licensed" not in c["text"].lower(),
                        rule="home_services/compliance: licence" if "licensed" in c["text"].lower() else None,
                        why="checked",
                    )
                    for c in body["claims"]
                ]
            )
        if schema is copy.Repairs:
            return copy.Repairs(fixes=[])  # nothing true to say instead: dropped
        raise AssertionError(schema)

    def edit_image(self, png, prompt, size):
        self._log(meta.IMAGE, "edit")
        if self.edit_delay:
            time.sleep(self.edit_delay)
        answer = {"1792x1024": (2016, 1152), "1024x1024": (1600, 1600)}[size]
        buf = io.BytesIO()
        Image.new("RGB", answer, (30, 60, 200)).save(buf, "PNG")
        return buf.getvalue()

    def segment(self, png, phrase):
        self._log(meta.SAM, "segment")
        if self.sam_empty:
            return []
        img = Image.open(io.BytesIO(png)).convert("RGB")
        mask = red_mask(img)
        box = mask.getbbox()
        if box is None:
            return []
        raster = mask.crop(box).point(lambda v: 1 if v else 0)
        return [meta.Segment(box, img.width, img.height, 0.95, raster.tobytes(), raster.width, raster.height)]


@pytest.fixture
def settings():
    return config.load()


@pytest.fixture
def launched(monkeypatch):
    """start's thread replaced: the test runs `run` itself, with the fake."""
    calls: list[str] = []
    monkeypatch.setattr(studio, "_launch", lambda settings, set_id: calls.append(set_id))
    return calls


def business(conn, biz="biz_roof", photos=(), knowledge=KNOWLEDGE, kinds=None) -> list[str]:
    conn.execute("INSERT INTO business (id) VALUES (%s)", (biz,))
    conn.execute(
        "INSERT INTO knowledge (business_id, version, json, source) VALUES (%s, 1, %s, 'owner')",
        (biz, Jsonb(knowledge)),
    )
    page = f"pg_{biz}"
    conn.execute(
        "INSERT INTO page (id, business_id, source, url, status) VALUES (%s, %s, 'admiry', '', 'published')",
        (page, biz),
    )
    ids = []
    for i, raw in enumerate(photos):
        img = Image.open(io.BytesIO(raw))
        pid = f"ph_{biz}_{i}"
        conn.execute(
            "INSERT INTO page_photo (id, page_id, bytes, content_type, width, height, kind, created_at) "
            "VALUES (%s, %s, %s, 'image/jpeg', %s, %s, %s, clock_timestamp())",
            (pid, page, raw, img.width, img.height, kinds[i] if kinds else None),
        )
        ids.append(pid)
    return ids


def make(conn, settings, launched, fake, biz="biz_roof") -> dict:
    first = studio.start(conn, settings, biz)
    assert first["status"] == "making" and launched == [first["set_id"]]
    studio.run(settings, first["set_id"], meta=fake)
    return studio.card(conn, biz, BASE)


def test_run_writes_ready_set_with_images(conn, settings, launched):
    business(conn, photos=[photo_bytes()])
    fake = FakeMeta()
    card = make(conn, settings, launched, fake)
    assert card["status"] == "ready", card
    assert set(card) >= {"status", "set_id", "steps", "images", "text", "removed", "models", "upload_url"}
    assert sorted(i["format"] for i in card["images"]) == ["landscape", "square"]
    for img in card["images"]:
        assert img["method"] == "extended", img["checks"]
        assert img["url"] == f"{BASE}/creatives/{img['id']}" and img["original_url"] == img["url"] + "/original"
        assert img["checks"]["iou"] >= 0.9 and img["checks"]["policy"]["ok"] is True
        assert len(img["checks"]["box"]) == 4 and len(img["checks"]["box_out"]) == 4
        assert img["id"].startswith("ci_")
    rows = conn.execute("SELECT format, width, height, bytes, content_type FROM ad_creative_image").fetchall()
    for row in rows:
        assert (row["width"], row["height"]) == images.SIZES[row["format"]]
        assert bytes(row["bytes"])[:3] == b"\xff\xd8\xff" and row["content_type"] == "image/jpeg"  # JPEG q90
    heads = card["text"]["headlines"]
    assert [h["text"] for h in heads] == ["Oakline Roofing", "Free Roof Inspections", "Roofers in Charlotte"]
    assert heads[1]["source"] == "Offer: Free roof inspections"
    assert {"text": "Licensed & Insured", "why": "checked (rule: home_services/compliance: licence)"} in card["removed"]
    assert card["steps"] and all(set(s) == {"step", "model", "ok", "ms"} for s in card["steps"])
    assert {s["model"] for s in card["steps"]} >= {meta.SPARK, meta.SAM, meta.IMAGE, meta.WONDER}
    assert card["models"]["check_text"] == meta.WONDER
    assert fake.count("edit") == 2


def test_privacy_flagged_photo_skipped(conn, settings, launched):
    ids = business(conn, photos=[photo_bytes(), photo_bytes()])
    fake = FakeMeta(privacy={ids[1]: ["house numbers"]})
    card = make(conn, settings, launched, fake)
    assert card["status"] == "ready"
    photos = {r["photo_id"] for r in conn.execute("SELECT photo_id FROM ad_creative_image").fetchall()}
    assert photos == {ids[0]}
    assert any("house numbers" in r["why"] and r["text"] == "Photo 2" for r in card["removed"])


def test_sam_empty_uses_plain_crop(conn, settings, launched):
    business(conn, photos=[photo_bytes()])
    fake = FakeMeta(sam_empty=True)
    card = make(conn, settings, launched, fake)
    assert card["status"] == "ready"
    assert [i["method"] for i in card["images"]] == ["plain_crop", "plain_crop"]
    assert fake.count("edit") == 0


def test_subject_inside_crop_uses_plain_crop_without_generating(conn, settings, launched):
    business(conn, photos=[photo_bytes(work=SMALL)])
    fake = FakeMeta()
    card = make(conn, settings, launched, fake)
    assert [i["method"] for i in card["images"]] == ["plain_crop", "plain_crop"]
    assert fake.count("edit") == 0
    assert all(i["checks"]["box"] == list(SMALL) for i in card["images"])


def test_small_photo_skipped_with_reason(conn, settings, launched):
    business(conn, photos=[photo_bytes(size=(280, 200), work=None), photo_bytes()])
    fake = FakeMeta()
    card = make(conn, settings, launched, fake)
    assert card["status"] == "ready"
    assert fake.count("PhotoRead") == 1  # the small one never went to Spark
    assert any(r["text"] == "Photo 1" and "small" in r["why"] for r in card["removed"])
    assert len(card["images"]) == 2


def test_failed_image_check_uses_plain_crop(conn, settings, launched):
    business(conn, photos=[photo_bytes()])
    fake = FakeMeta(image_ok=False)
    card = make(conn, settings, launched, fake)
    assert card["status"] == "ready"
    assert [i["method"] for i in card["images"]] == ["plain_crop", "plain_crop"]
    for img in card["images"]:
        assert img["checks"]["policy"] == {"ok": False, "problems": ["a visible seam"]}
        assert img["checks"]["fallback"]
    assert fake.count("edit") == 2


def test_accept_then_accepted_returns_kept_text_and_images(conn, settings, launched):
    business(conn, photos=[photo_bytes()])
    card = make(conn, settings, launched, FakeMeta())
    assert studio.accepted(conn, "biz_roof") is None
    done = studio.accept(conn, "biz_roof", card["set_id"])
    assert done["status"] == "accepted" and done["set_id"] == card["set_id"]
    got = studio.accepted(conn, "biz_roof")
    assert got["set_id"] == card["set_id"]
    assert got["headlines"] == ["Oakline Roofing", "Free Roof Inspections", "Roofers in Charlotte"]
    assert len(got["descriptions"]) == 2 and got["callouts"] == ["Free Inspections"]
    assert {i["format"] for i in got["images"]} == {"landscape", "square"}
    assert all(isinstance(i["bytes"], bytes) and i["bytes"][:3] == b"\xff\xd8\xff" for i in got["images"])  # JPEG
    assert studio.card(conn, "biz_roof", BASE)["status"] == "accepted"


def test_accept_rejects_set_of_other_business(conn, settings, launched):
    business(conn, photos=[photo_bytes()])
    business(conn, biz="biz_other", photos=[photo_bytes()])
    card = make(conn, settings, launched, FakeMeta())
    with pytest.raises(ToolError):
        studio.accept(conn, "biz_other", card["set_id"])
    assert studio.accepted(conn, "biz_other") is None
    assert studio.card(conn, "biz_roof", BASE)["status"] == "ready"


def test_start_without_photos_needs_photos_and_starts_nothing(conn, settings, launched):
    business(conn, photos=[])
    card = studio.start(conn, settings, "biz_roof")
    assert card["status"] == "needs_photos" and card["set_id"] is None
    # R3 corrected: only create_page and build_site mint upload links; without a valid one the card says which tool
    # gives the owner a fresh one, and mints none itself
    assert card["upload_url"] is None
    assert 'update_landing_page with action "build_site"' in card["error"]
    assert conn.execute("SELECT count(*) AS n FROM upload_token").fetchone()["n"] == 0
    assert launched == []
    assert conn.execute("SELECT count(*) AS n FROM ad_creative_set").fetchone()["n"] == 0


def test_needs_photos_card_reads_the_valid_upload_link_back(conn, settings, launched):
    from agent import uploads

    business(conn, photos=[])
    token = uploads.new_token(conn, "pg_biz_roof")
    card = studio.start(conn, settings, "biz_roof")
    assert card["status"] == "needs_photos" and card["upload_url"] == f"{BASE}/u/{token}"
    assert card["error"]
    assert conn.execute("SELECT count(*) AS n FROM upload_token").fetchone()["n"] == 1


def test_start_inside_an_open_transaction_the_run_sees_its_set(conn, settings, monkeypatch):
    # the MCP wrapper runs a tool's body inside `with conn.transaction()`: the run's own connection sees the set only
    # once that commits, so the run waits for it rather than giving up on a row it cannot see yet
    business(conn, photos=[photo_bytes(work=SMALL)])
    fake, threads = FakeMeta(), []

    def launch(settings, set_id):
        t = threading.Thread(target=studio.run, args=(settings, set_id), kwargs={"meta": fake}, daemon=True)
        threads.append(t)
        t.start()

    monkeypatch.setattr(studio, "_launch", launch)
    with conn.transaction():
        first = studio.start(conn, settings, "biz_roof")
        time.sleep(0.5)  # the run has looked for its set by now, and must not have given up on it
    threads[0].join(timeout=30)
    assert not threads[0].is_alive()
    assert studio.card(conn, "biz_roof", BASE)["status"] == "ready" and first["set_id"]


def test_writer_gets_the_knowledge_files(conn, settings, launched):
    business(conn, photos=[photo_bytes(work=SMALL)])
    fake = FakeMeta()
    make(conn, settings, launched, fake)
    sent = " ".join(p["text"] for p in fake.parts["AdText"][0] if p["type"] == "text")
    for name in ("compliance.md", "roofing.md", "google-search.md", "offers.md"):
        body = studio._body(studio.RULES_DIR / name)
        assert body[:200] in sent, name


def test_wonder_ok_with_problems_is_a_failure(conn, settings, launched):
    business(conn, photos=[photo_bytes()])
    card = make(conn, settings, launched, FakeMeta(image_check="ok_with_problems"))
    assert [i["method"] for i in card["images"]] == ["plain_crop", "plain_crop"]
    assert "Google's image rules" in card["images"][0]["checks"]["fallback"]


def test_unanswered_image_check_says_it_could_not_be_checked(conn, settings, launched):
    business(conn, photos=[photo_bytes()])
    card = make(conn, settings, launched, FakeMeta(image_check="down"))
    assert [i["method"] for i in card["images"]] == ["plain_crop", "plain_crop"]
    for img in card["images"]:
        assert "could not be checked" in img["checks"]["fallback"]
        assert "Google's image rules" not in img["checks"]["fallback"]


def test_an_error_while_finishing_fails_the_set(conn, settings, launched, monkeypatch):
    business(conn, photos=[photo_bytes(work=SMALL)])

    def broken(img):
        raise OSError("disk full")

    monkeypatch.setattr(studio.images, "encode", broken)
    card = make(conn, settings, launched, FakeMeta())
    assert card["status"] == "failed" and card["error"] == studio.BROKE
    assert conn.execute("SELECT count(*) AS n FROM ad_creative_image").fetchone()["n"] == 0


def test_all_photos_skipped_fails_the_set_with_the_reasons(conn, settings, launched):
    ids = business(conn, photos=[photo_bytes(), photo_bytes(size=(280, 200), work=None)])
    card = make(conn, settings, launched, FakeMeta(privacy={ids[0]: ["house numbers"]}))
    assert card["status"] == "failed" and card["images"] == []
    assert "house numbers" in card["error"] and "too small" in card["error"]
    assert {r["text"] for r in card["removed"]} >= {"Photo 1", "Photo 2"}


def test_photo_choice_prefers_work_before_after_and_key(conn):
    kinds = ["team", "place", "work", "key", "before_after", "work", "before_after"]
    ids = business(conn, photos=[photo_bytes(work=SMALL)] * len(kinds), kinds=kinds)
    chosen = [r["id"] for r in studio._photo_rows(conn, "biz_roof")]
    assert chosen == [ids[2], ids[5], ids[4], ids[6], ids[3]]


def test_creative_images_are_indexed_by_set(conn):
    names = {
        r["indexname"] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'ad_creative_image'")
    }
    assert "ad_creative_image_set" in names


def test_start_while_making_starts_no_second_run(conn, settings, launched):
    business(conn, photos=[photo_bytes()])
    first = studio.start(conn, settings, "biz_roof")
    again = studio.start(conn, settings, "biz_roof")
    assert again["set_id"] == first["set_id"] and again["status"] == "making"
    assert launched == [first["set_id"]]


def test_run_gives_up_after_the_deadline(conn, settings, launched, monkeypatch):
    business(conn, photos=[photo_bytes()])
    monkeypatch.setattr(studio, "GIVE_UP_S", 0.5)
    first = studio.start(conn, settings, "biz_roof")
    studio.run(settings, first["set_id"], meta=FakeMeta(edit_delay=2.0))
    card = studio.card(conn, "biz_roof", BASE)
    assert card["status"] == "failed" and "4 minutes" in card["error"]


def test_text_that_fails_to_write_fails_the_set(conn, settings, launched):
    business(conn, photos=[photo_bytes(work=SMALL)])

    class NoWriter(FakeMeta):
        def json(self, model, system, parts, schema):
            if schema is copy.AdText:
                raise meta.MetaError("muse-spark-1.3 is unavailable (503)")
            return super().json(model, system, parts, schema)

    card = make(conn, settings, launched, NoWriter())
    assert card["status"] == "failed" and card["error"]
    assert all(s["ok"] is False for s in card["steps"] if s["step"].startswith("write_text"))
