"""The photo kinds of an industry site, the slots they fill and the checklist (A3p-6 batch 2 Task 1; plan
2026-10-06-agent-a3p-6-2-facts-photos-publish Q7, Q8, Q11; spec T11-T13): what the owner says each photo shows, which
slot of the site it goes to, how many of each the site uses, what is still to upload, and the sentence that says what
keeps the site from going live. A photo with no kind (a starter page's) is never placed, and a page never lists
another page's photos."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from psycopg.types.json import Jsonb

from agent import landing
from agent.sites import families, images, intake, photos
from agent.sites.spec import Facts, ImageRef, Service, SiteSpec
from agent.tests.conftest import ACCOUNT_JARGON, JARGON, MONEY_JARGON

HOME, DENTAL = "home_services", "dental"


def _page(conn, biz: str = "biz_1") -> str:
    conn.execute("INSERT INTO business (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (biz,))
    return landing.ensure_page_for_kit(conn, biz)["id"]


def _photo(conn, page_id: str, photo_id: str, kind: str | None, minute: int = 0) -> None:
    """A photo of `kind` (None: one a starter page took), uploaded `minute` minutes after noon."""
    conn.execute(
        "INSERT INTO page_photo (id, page_id, bytes, content_type, width, height, kind, created_at) "
        "VALUES (%s, %s, '\\x00', 'image/jpeg', 1, 1, %s, %s)",
        (photo_id, page_id, kind, datetime(2026, 10, 6, 12, minute, tzinfo=timezone.utc)),
    )


def _owner(photo_id: str) -> ImageRef:
    return ImageRef(source="owner", id=photo_id, alt="")


# ---------------------------------------------------------------- Q7: the kinds


def test_the_kinds_of_each_family_are_the_plans_in_the_checklists_order_with_their_needs():
    assert [(k["id"], k["label"], k["need"]) for k in photos.KINDS[HOME]] == [
        ("logo", "Your logo", 1),
        ("key", "One photo of your own work, your team or your truck", 1),
        ("place", "Your shop or yard", 1),
        ("team", "You and your team", 1),
        ("work", "More of your work", 3),
        ("before_after", "Before and after", 2),
    ]
    assert [(k["id"], k["label"], k["need"]) for k in photos.KINDS[DENTAL]] == [
        ("logo", "Your logo", 1),
        ("key", "Your office, outside or in", 1),
        ("place", "The waiting room or a treatment room", 2),
        ("team", "You and your team", 1),
        ("dentist", "The dentist", 1),
    ]
    assert set(photos.KINDS) == set(families.FAMILIES)
    assert photos.MAX_SITE_PHOTOS == {HOME: 9, DENTAL: 6}  # each family's needs summed
    assert photos.KIND_IDS == ("logo", "key", "place", "team", "work", "before_after", "dentist")


def test_every_slot_a_kind_fills_is_one_the_family_reads_and_the_key_photo_is_its_key_slot():
    for family_id, by_kind in photos.SLOTS.items():
        family = families.get(family_id)
        spec = SiteSpec(
            family=family_id,
            style=next(iter(family.styles)),
            facts=Facts(name="X", services=[Service(slug="roof-repair", name="Roof repair", lead=True)]),
        )
        readable = {*family.slot_kinds(spec), *images.PEOPLE_SLOTS, "logo"}
        assert {kind["id"] for kind in photos.KINDS[family_id]} == set(by_kind)
        for kind, slots in by_kind.items():
            assert set(slots) <= readable, (family_id, kind)
        assert by_kind["key"][0] == family.key_photo_slot  # the key photo is the one the family's gate is about


def test_the_words_of_the_kinds_pass_the_jargon_tests():
    for kinds in photos.KINDS.values():
        for kind in kinds:
            assert not (JARGON.search(kind["label"]) or ACCOUNT_JARGON.search(kind["label"]))
            assert not MONEY_JARGON.search(kind["label"])


# ---------------------------------------------------------------- the page's own: is it a site, and of which family


def test_a_page_is_a_site_by_its_draft_or_by_its_spec():
    assert photos.is_site({"site_draft": {"family": HOME}}) is True
    assert photos.is_site({"site_draft": {}}) is True  # a draft nothing has been said into yet
    assert photos.is_site({"site_draft": None, "site_spec_id": "ss_1"}) is True
    assert photos.is_site({"site_spec_id": "ss_1"}) is True
    assert photos.is_site({"site_draft": None, "site_spec_id": None}) is False
    assert photos.is_site({"id": "pg_1"}) is False
    assert photos.is_site(None) is False


def test_the_family_of_a_page_is_its_drafts_else_the_live_previews_sections():
    assert photos.family_of({"site_draft": {"family": DENTAL}}) == DENTAL
    assert photos.family_of({"site_draft": {"family": DENTAL}, "sections": {"family": HOME}}) == DENTAL
    assert photos.family_of({"site_draft": None, "sections": {"kind": "site", "family": HOME}}) == HOME
    assert photos.family_of({"site_draft": {"family": None}, "sections": {"kind": "site", "family": DENTAL}}) == DENTAL
    assert photos.family_of({"site_draft": {"style": "solid"}, "site_spec_id": "ss_1"}) is None
    assert photos.family_of({"site_draft": None, "sections": None}) is None
    assert photos.family_of({"site_draft": None, "sections": {"copy": {}}}) is None  # a starter page's sections
    assert photos.family_of({"site_draft": {"family": "attorneys"}}) is None  # not a family of sites
    assert photos.family_of({"id": "pg_1"}) is None
    assert photos.family_of(None) is None


def test_a_landing_row_carries_no_sections_so_only_its_draft_names_the_family(conn):
    """`landing.PAGE_COLS` leaves `sections` out: the fallback reads them only from a row the caller selected whole."""
    page_id = _page(conn)
    conn.execute("UPDATE page SET sections = %s WHERE id = %s", (Jsonb({"kind": "site", "family": DENTAL}), page_id))
    assert "sections" not in landing.get(conn, page_id)
    assert photos.family_of(landing.get(conn, page_id)) is None
    assert photos.family_of(dict(conn.execute("SELECT * FROM page WHERE id = %s", (page_id,)).fetchone())) == DENTAL


def test_a_page_row_from_landing_is_read_the_same_way(conn):
    page_id = _page(conn)
    assert photos.is_site(landing.get(conn, page_id)) is False
    intake.save_draft(conn, page_id, {**intake.draft(None), "family": DENTAL})
    row = landing.current(conn, "biz_1")
    assert photos.is_site(row) is True and photos.family_of(row) == DENTAL


# ---------------------------------------------------------------- Q8: the images


def test_the_owners_photos_are_named_by_slot_and_a_photo_with_no_kind_is_never_placed(conn):
    page_id = _page(conn)
    _photo(conn, page_id, "ph_starter", None, 0)  # a starter page's: it has no kind
    _photo(conn, page_id, "ph_logo", "logo", 1)
    _photo(conn, page_id, "ph_key", "key", 2)
    _photo(conn, page_id, "ph_work_1", "work", 3)
    _photo(conn, page_id, "ph_work_2", "work", 4)
    assert photos.images(conn, page_id, HOME) == {
        "logo": _owner("ph_logo"),
        "hero": _owner("ph_key"),
        "gallery-1": _owner("ph_work_1"),
        "gallery-2": _owner("ph_work_2"),
    }
    assert "ph_starter" not in {ref.id for ref in photos.images(conn, page_id, HOME).values()}
    assert all(
        ref.source == "owner" and ref.alt == "" and ref.label is False
        for ref in photos.images(conn, page_id, HOME).values()
    )


def test_a_home_services_place_team_and_gallery_photos_go_where_the_template_reads_them(conn):
    page_id = _page(conn)
    _photo(conn, page_id, "ph_yard", "place", 1)
    _photo(conn, page_id, "ph_crew", "team", 2)
    _photo(conn, page_id, "ph_w1", "work", 3)
    _photo(conn, page_id, "ph_ba1", "before_after", 4)
    _photo(conn, page_id, "ph_w2", "work", 5)
    _photo(conn, page_id, "ph_w3", "work", 6)  # the fourth photo has no slot among the gallery's three
    placed = photos.images(conn, page_id, HOME)
    assert placed == {
        "about": _owner("ph_yard"),
        "team": _owner("ph_crew"),
        "gallery-1": _owner("ph_w1"),
        "gallery-2": _owner("ph_ba1"),  # work and before-and-after share the gallery, in the order they came
        "gallery-3": _owner("ph_w2"),
    }


def test_a_dental_practices_photos_go_to_its_slots_and_the_key_photo_is_the_first_of_the_office(conn):
    page_id = _page(conn)
    _photo(conn, page_id, "ph_logo", "logo", 1)
    _photo(conn, page_id, "ph_office", "key", 2)
    _photo(conn, page_id, "ph_wait", "place", 3)
    _photo(conn, page_id, "ph_room", "place", 4)
    _photo(conn, page_id, "ph_team", "team", 5)
    _photo(conn, page_id, "ph_dr", "dentist", 6)
    _photo(conn, page_id, "ph_work", "work", 7)  # a kind of the other family: no slot of a dental site
    assert photos.images(conn, page_id, DENTAL) == {
        "logo": _owner("ph_logo"),
        "office-1": _owner("ph_office"),
        "office-2": _owner("ph_wait"),
        "contact": _owner("ph_room"),
        "team": _owner("ph_team"),
        "dentist": _owner("ph_dr"),
    }


def test_the_photos_of_another_page_are_never_listed_and_an_unknown_family_places_nothing(conn):
    mine, theirs = _page(conn, "biz_1"), _page(conn, "biz_2")
    _photo(conn, mine, "ph_mine", "key", 1)
    _photo(conn, theirs, "ph_theirs_key", "key", 2)
    _photo(conn, theirs, "ph_theirs_logo", "logo", 3)
    assert photos.images(conn, mine, HOME) == {"hero": _owner("ph_mine")}
    assert photos.images(conn, theirs, HOME) == {"logo": _owner("ph_theirs_logo"), "hero": _owner("ph_theirs_key")}
    assert photos.images(conn, mine, "attorneys") == {}
    assert photos.images(conn, _page(conn, "biz_3"), HOME) == {}


def test_the_photos_of_one_upload_keep_their_order_by_id_when_they_came_at_the_same_moment(conn):
    page_id = _page(conn)
    for n in (3, 1, 2):  # one transaction's rows share a time: the id says which was first
        _photo(conn, page_id, f"ph_w{n}", "work", 0)
    assert photos.images(conn, page_id, HOME) == {
        "gallery-1": _owner("ph_w1"),
        "gallery-2": _owner("ph_w2"),
        "gallery-3": _owner("ph_w3"),
    }


# ---------------------------------------------------------------- Q8: the checklist


def test_the_checklist_counts_what_is_on_file_and_what_is_done(conn):
    page_id = _page(conn)
    _photo(conn, page_id, "ph_key", "key", 1)
    _photo(conn, page_id, "ph_yard", "place", 2)
    _photo(conn, page_id, "ph_w1", "work", 3)
    _photo(conn, page_id, "ph_w2", "work", 4)
    _photo(conn, page_id, "ph_old", None, 5)
    listed = photos.checklist(conn, page_id, HOME, intake.draft(None))
    assert [(c["id"], c["label"], c["need"], c["have"], c["done"], c["blocks_publish"]) for c in listed] == [
        ("logo", "Your logo", 1, 0, False, True),
        ("key", "One photo of your own work, your team or your truck", 1, 1, True, True),
        ("place", "Your shop or yard", 1, 1, True, False),
        ("team", "You and your team", 1, 0, False, False),
        ("work", "More of your work", 3, 2, False, False),
        ("before_after", "Before and after", 2, 0, False, False),
    ]
    assert all(set(c) == {"id", "label", "need", "have", "done", "blocks_publish"} for c in listed)


def _logo_line(conn, page_id: str, draft: dict) -> tuple[int, bool]:
    """The checklist's first line, the logo's: how many are on file and whether it is done."""
    line = photos.checklist(conn, page_id, DENTAL, draft)[0]
    assert line["id"] == "logo"
    return line["have"], line["done"]


def test_a_logo_is_done_with_a_logo_or_with_the_wordmark_the_owner_accepted(conn):
    page_id = _page(conn)
    accepted = {**intake.draft(None), "wordmark_ok": True}
    assert _logo_line(conn, page_id, intake.draft(None)) == (0, False)
    assert _logo_line(conn, page_id, accepted) == (0, True)  # nothing uploaded, nothing owed
    _photo(conn, page_id, "ph_logo", "logo", 1)
    assert _logo_line(conn, page_id, intake.draft(None)) == (1, True)
    assert all(not c["done"] for c in photos.checklist(conn, page_id, DENTAL, accepted)[1:])  # only the logo's


def test_the_checklist_of_a_dental_site_has_the_dental_kinds_and_none_of_another_pages_photos(conn):
    mine, theirs = _page(conn, "biz_1"), _page(conn, "biz_2")
    _photo(conn, mine, "ph_wait", "place", 1)
    _photo(conn, theirs, "ph_wait_2", "place", 2)
    _photo(conn, theirs, "ph_room_2", "place", 3)
    listed = photos.checklist(conn, mine, DENTAL, None)  # no draft at all reads as an empty one
    assert [(c["id"], c["have"], c["need"], c["done"]) for c in listed] == [
        ("logo", 0, 1, False),
        ("key", 0, 1, False),
        ("place", 1, 2, False),
        ("team", 0, 1, False),
        ("dentist", 0, 1, False),
    ]
    assert [c["blocks_publish"] for c in listed] == [True, True, False, False, False]


# ---------------------------------------------------------------- Q11: the key photos


def test_the_gate_sentences_are_the_plans_words():
    assert photos.GATE == {
        HOME: {
            "both": "Your site needs your logo and one photo of your own work before it goes live.",
            "logo": "Your site needs your logo before it goes live.",
            "key": "Your site needs one photo of your own work before it goes live.",
        },
        DENTAL: {
            "both": "Your site needs your logo and one photo of your office before it goes live.",
            "logo": "Your site needs your logo before it goes live.",
            "key": "Your site needs one photo of your office before it goes live.",
        },
    }
    assert len({s for cases in photos.GATE.values() for s in cases.values()}) == 5  # the logo's is the one both say
    for sentence in (s for cases in photos.GATE.values() for s in cases.values()):
        assert not (JARGON.search(sentence) or ACCOUNT_JARGON.search(sentence) or MONEY_JARGON.search(sentence))


@pytest.mark.parametrize(
    ("family", "logo", "key", "wordmark", "sentence"),
    [
        (HOME, False, False, False, "Your site needs your logo and one photo of your own work before it goes live."),
        (HOME, False, True, False, "Your site needs your logo before it goes live."),
        (HOME, True, False, False, "Your site needs one photo of your own work before it goes live."),
        (HOME, True, True, False, None),
        (HOME, False, True, True, None),  # the wordmark the owner accepted stands for the logo
        (HOME, False, False, True, "Your site needs one photo of your own work before it goes live."),
        (HOME, True, False, True, "Your site needs one photo of your own work before it goes live."),
        (DENTAL, False, False, False, "Your site needs your logo and one photo of your office before it goes live."),
        (DENTAL, False, True, False, "Your site needs your logo before it goes live."),
        (DENTAL, True, False, False, "Your site needs one photo of your office before it goes live."),
        (DENTAL, True, True, False, None),
        (DENTAL, False, True, True, None),
        (DENTAL, False, False, True, "Your site needs one photo of your office before it goes live."),
    ],
)
def test_the_key_photos_say_what_keeps_a_site_from_going_live(conn, family, logo, key, wordmark, sentence):
    page_id = _page(conn)
    if logo:
        _photo(conn, page_id, "ph_logo", "logo", 1)
    if key:
        _photo(conn, page_id, "ph_key", "key", 2)
    _photo(conn, page_id, "ph_work", "work" if family == HOME else "place", 3)  # not a key photo
    draft = {**intake.draft(None), "wordmark_ok": wordmark}
    assert photos.key_photos(conn, page_id, family, draft) == {
        "ok": sentence is None,
        "logo": logo,
        "key": key,
        "wordmark_ok": wordmark,
        "sentence": sentence,
    }


def test_key_photos_calls_a_family_that_is_none_of_ours_an_error_of_the_caller_not_a_missing_key(conn):
    page_id = _page(conn)
    with pytest.raises(ValueError, match="no site family called 'attorneys'"):
        photos.key_photos(conn, page_id, "attorneys", None)
    _photo(conn, page_id, "ph_logo", "logo", 1)
    _photo(conn, page_id, "ph_key", "key", 2)  # even when nothing would be missing
    with pytest.raises(ValueError, match="no site family called 'attorneys'"):
        photos.key_photos(conn, page_id, "attorneys", None)
    assert photos.key_photos(conn, page_id, HOME, None)["ok"] is True


def test_a_key_photo_of_another_page_is_nobodys_but_its_own(conn):
    mine, theirs = _page(conn, "biz_1"), _page(conn, "biz_2")
    _photo(conn, theirs, "ph_logo", "logo", 1)
    _photo(conn, theirs, "ph_key", "key", 2)
    assert photos.key_photos(conn, mine, HOME, None)["ok"] is False
    assert photos.key_photos(conn, theirs, HOME, None)["ok"] is True


# ---------------------------------------------------------------- was the site built (Task 5's follow-up)


@pytest.mark.parametrize(
    ("draft", "built"),
    [
        ({"family": HOME, "style": "solid", "words": {"home": {}}}, True),
        ({"family": DENTAL, "style": "boutique", "words": {"home": {}}}, True),
        ({"family": HOME, "style": "solid", "words": None}, False),  # never built: the photos join the first build
        ({"family": HOME, "style": None, "words": {"home": {}}}, False),  # no look
        ({"family": HOME, "style": "calm", "words": {"home": {}}}, False),  # a look of the other family: none
        ({"family": None, "style": "solid", "words": {"home": {}}}, False),
        ({"family": "attorneys", "style": "solid", "words": {"home": {}}}, False),
    ],
)
def test_a_site_was_built_when_it_has_words_a_family_of_ours_and_a_look_of_that_family(conn, draft, built):
    """The one reading of "built" the upload link and the photos' rebuild share: a change of the photos has a preview to
    change only for a site whose words a build kept, under a look its own family has."""
    page_id = _page(conn)
    intake.save_draft(conn, page_id, {**intake.draft(None), **draft})
    assert photos.was_built(landing.get(conn, page_id)) is built
    assert photos.was_built(None) is False and photos.was_built({"id": "pg_1"}) is False


# ---------------------------------------------------------------- the rebuild of a page that names no family (Task 4)


@pytest.mark.parametrize("family", [None, "attorneys"])
def test_a_page_that_names_no_family_of_ours_is_not_built_again_and_nothing_is_logged(conn, caplog, family):
    """Every caller of `key_photos` asks for the page's family first (Task 4 follow-up): a draft with words and no family
    of ours is no site to build -- nothing is built, and nothing is wrong for the operator to look at."""
    page_id = _page(conn)
    intake.save_draft(conn, page_id, {**intake.draft(None), "family": family, "style": "solid", "words": {"home": {}}})
    with caplog.at_level(logging.ERROR, logger="agent.sites.photos"):
        assert photos.rebuild(page_id) is None
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
