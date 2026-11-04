"""The rival-name guard (capability layer K4, plan D5; the final review's I1): a business nearby is described, never
named. Its names are the forms of its Google Maps name and of the title of its page when one was read -- the whole and
each part, a part without the trade's last words or a legal suffix, a person's name without the honorific -- read with
accents, case, & and a possessive aside. A name made only of the words any business of the trade here has (its trade,
its services, its town, a few service words) is a description: it names a business only written as a proper noun
inside a sentence, never in a search. A town names no business. The rivals are the brief's: dental ones beside a
dentist in Palo Alto, plumbing ones beside a plumber there, so the trade words are the business's own."""

from __future__ import annotations

import json

import pytest

from agent import config, crawl, models, places
from agent.roles import research_material as rm
from agent.roles import researcher
from agent.tests import research_fixtures as rf
from agent.tests.fixtures import home_services_knowledge, knowledge_fixture

MASK = rm.NEARBY_MASK
PA_FAMILY = "https://pafamily.example"  # the first dental rival's website: its page is titled with a brand of its own
ACME = "https://acme.example"  # the third plumbing rival's: its title names it, its Places name does not
# the coordinator's ruling on concern (a): a title's part is the brand only when it shares a word with its business's
# Places name or its website's host; a tagline, or a rebranded title, names only as a proper noun
DOWNTOWN = "https://downtowndental.com"
OLD_MILL = "https://oldmilldental.example"
ACME_PLUMBING = "https://acme-plumbing.example"
# the re-review's cases: a dentist named in a title after a word, a short word shared, a host named for the town
UNIVERSITY = "https://universitydentalcare.example"
NEW_SMILE = "https://newsmiledental.com"
BRIGHT_NOW = "https://paloalto.brightnow.com"
TITLES = {
    PA_FAMILY: "PA Family Dentistry",
    ACME: "Acme Rooter | Plumbing in Palo Alto",
    DOWNTOWN: "New Patients | Downtown Dental",
    OLD_MILL: "Zenith Smiles | Family Dentist",
    ACME_PLUMBING: "Acme Rooter | Plumbing in Palo Alto",
    UNIVERSITY: "Meet Dr. Raj Patel - University Dental Care",
    NEW_SMILE: "New Patients | New Smile Dental",
    BRIGHT_NOW: "Teeth Whitening Palo Alto | Bright Now! Dental",
}
DENTAL = (
    "Palo Alto Family Dentistry - Dr. Jane Doe, DDS",
    "Bright Smile Dental Group of Palo Alto",
    "Dr. Lee's Orthodontics | Palo Alto",
    "Smith & Jones Dental",
    "Smile",
    "Clínica Dental Pérez",
)
PLUMBING = ("Emergency Plumber", "Palo Alto Plumbing", "Bay Drain Experts", "Water Heater Repair")


def _gathered(
    conn, monkeypatch, knowledge: dict, rivals: tuple[str, ...], sites: dict[int, str] | None = None
) -> rm.Gathered:
    """The material of a business in Palo Alto with `knowledge` beside the businesses nearby named `rivals` (rival k's
    website is sites[k]), each page served with its title from TITLES."""
    rf.seed(conn, knowledge={**knowledge, "listing": rf.LISTING})
    rf.no_guides(monkeypatch)
    found = [
        places.Candidate(
            f"ChIJr{k}", name, f"{k} Main St, Palo Alto, CA", 37.45, -122.143, website=(sites or {}).get(k)
        )
        for k, name in enumerate(rivals, 1)
    ]
    rf.fake_places(monkeypatch, found)
    monkeypatch.setattr(
        rm,
        "fetch_page",
        lambda url: crawl.Page(url, TITLES[url], "Free exams for new patients.") if url in TITLES else None,
    )
    return rm.gather(conn, config.load(), rf.BIZ, {})


def _dentist(conn, monkeypatch) -> rm.Gathered:
    """A family dentist in Palo Alto (Invisalign, cleanings) beside the dental rivals."""
    knowledge = {**knowledge_fixture().model_dump(), "name": "Hillside Dental"}
    return _gathered(conn, monkeypatch, knowledge, DENTAL, {1: PA_FAMILY})


def _plumber(conn, monkeypatch) -> rm.Gathered:
    """A plumber in Palo Alto (drain cleaning, water heater repair) beside the plumbing rivals."""
    knowledge = {
        **home_services_knowledge("Drain cleaning"),
        "name": "Summit Plumbing",
        "category_detail": "plumber",
        "services": [
            {"name": "Drain cleaning", "intent_phrases": [], "price_hint": None},
            {"name": "Water heater repair", "intent_phrases": [], "price_hint": None},
        ],
    }
    return _gathered(conn, monkeypatch, knowledge, PLUMBING, {3: ACME})


def _brief(**fields) -> models.ResearchBrief:
    empty = {"findings": [], "suggested_themes": [], "suggested_negatives": [], "offer_angles": [], "unknowns": []}
    return models.ResearchBrief(**{**empty, **fields, "grounded": True})


def _finding(text: str) -> models.ResearchFinding:
    return models.ResearchFinding(text=text, source_ids=["business"], confidence="medium")


def _dropped(g: rm.Gathered, text: str, *, search: bool) -> str | None:
    """Why the guards drop `text` as a search to aim for, or as a finding resting on the business's own source."""
    brief = _brief(suggested_themes=[text]) if search else _brief(findings=[_finding(text)])
    stored, _ = researcher._guarded(brief, g)
    return (stored["suggested_themes"] if search else stored["findings"])[0]["dropped"]


NAMED_DENTAL = [
    ("Dr. Jane Doe offers free exams.", False),  # the person after the dash
    ("Jane Doe's office is open on Saturdays.", False),  # without the honorific, a possessive after it
    ("smith and jones dental", True),  # & written as and
    ("Smith & Jones Dental offers free whitening.", False),
    ("Smith and Jones offers payment plans.", False),  # without the trade's last word
    ("bright smile dental group palo alto", True),  # the part before " of "
    ("Dr. Lee's Orthodontics offers payment plans.", False),  # the part before " | "
    ("Lee Orthodontics offers payment plans.", False),  # the part without its honorific
    ("Dr. Lee offers payment plans.", False),  # the honorific with the person's name
    ("Palo Alto Family Dentistry offers free exams.", False),  # words any dentist here has, written as a name
    ("PA Family Dentistry offers free exams.", False),  # the title of its page
    ("Clinica Dental Perez abre los sabados.", False),  # an accented name written without its accents
    ("Clínica Dental Pérez abre los sábados.", False),
    ("Smile makeovers are popular nearby.", False),  # a one-word name written with a capital, even first (N1)
]
KEPT_DENTAL = [
    ("Patients here want a brighter smile.", False),  # "Smile" is a name only written as one
    ("family dentistry palo alto", True),
    ("palo alto family dentistry", True),  # a description in a search, whoever is called that
    ("Most practices nearby promise a bright smile.", False),
    ("Most practices in Palo Alto open on Saturdays.", False),  # a town is not a business
]


@pytest.mark.parametrize("text, search", NAMED_DENTAL)
def test_a_dental_rival_named_in_any_of_its_forms_is_dropped(conn, monkeypatch, text, search):
    assert _dropped(_dentist(conn, monkeypatch), text, search=search) == researcher.NAMED


@pytest.mark.parametrize("text, search", KEPT_DENTAL)
def test_what_names_no_dental_rival_is_kept(conn, monkeypatch, text, search):
    assert _dropped(_dentist(conn, monkeypatch), text, search=search) is None


NAMED_PLUMBING = [
    ("Acme Rooter offers $50 off.", False),  # the title of its page alone names it
    ("acme rooter drain cleaning", True),
    ("Emergency Plumber offers free estimates.", False),  # a name of trade words, written as a name
    ("Ask Palo Alto Plumbing for a quote.", False),
]
KEPT_PLUMBING = [
    ("emergency plumber palo alto", True),  # the business's own search, whatever a rival is called
    ("plumbing repair palo alto", True),
    ("palo alto plumbing", True),
    ("water heater repair near me", True),  # its own service's words
    ("People here search for an emergency plumber at night.", False),
    ("Plumbing in Palo Alto is busiest in winter.", False),  # the first word is capitalised as a sentence's is
]


@pytest.mark.parametrize("text, search", NAMED_PLUMBING)
def test_a_plumbing_rival_named_in_any_of_its_forms_is_dropped(conn, monkeypatch, text, search):
    assert _dropped(_plumber(conn, monkeypatch), text, search=search) == researcher.NAMED


@pytest.mark.parametrize("text, search", KEPT_PLUMBING)
def test_a_plumbers_own_searches_are_kept_whatever_its_rivals_are_called(conn, monkeypatch, text, search):
    assert _dropped(_plumber(conn, monkeypatch), text, search=search) is None


def _titled(conn, monkeypatch) -> rm.Gathered:
    """The dentist beside Downtown Dental Care (downtowndental.com, titled "New Patients | Downtown Dental") and Old Mill
    Dental, whose page is titled with a new brand ("Zenith Smiles | Family Dentist")."""
    knowledge = {**knowledge_fixture().model_dump(), "name": "Hillside Dental"}
    rivals = ("Downtown Dental Care", "Old Mill Dental")
    return _gathered(conn, monkeypatch, knowledge, rivals, {1: DOWNTOWN, 2: OLD_MILL})


@pytest.mark.parametrize(
    "text, search, dropped",
    [
        ("Most nearby practices welcome new patients.", False, None),  # a tagline in a title is no brand ...
        ("new patients dentist palo alto", True, None),  # ... so a top dental search stays
        ("Downtown Dental offers free exams.", False, researcher.NAMED),  # the title's part its host and name share
        ("downtown dental palo alto", True, researcher.NAMED),
        ("Zenith Smiles offers whitening.", False, researcher.NAMED),  # a new brand written as a proper noun ...
        ("zenith smiles", True, None),  # ... and in a search the Critic's `unnamed` check is the backstop
    ],
)
def test_a_part_of_a_title_is_a_brand_only_when_it_shares_a_word_with_its_business(
    conn, monkeypatch, text, search, dropped
):
    """The coordinator's ruling on concern (a): a part of a page's title names its business wherever it is written only
    when it shares a word that is not generic with the business's Places name or with its website's host (a word of
    four letters or more found in the host's first label, or the part's words joined); any other part names it only
    written as a proper noun inside a sentence, never in a search."""
    assert _dropped(_titled(conn, monkeypatch), text, search=search) == dropped


def test_a_title_part_sharing_the_places_name_is_the_brand_in_a_search(conn, monkeypatch):
    knowledge = {
        **home_services_knowledge("Drain cleaning"),
        "name": "Summit Plumbing",
        "category_detail": "plumber",
    }
    g = _gathered(conn, monkeypatch, knowledge, ("Acme Rooter Plumbing",), {1: ACME_PLUMBING})
    assert _dropped(g, "acme rooter palo alto", search=True) == researcher.NAMED


def test_a_titles_parts_are_read_by_what_they_share_with_their_business():
    names = rm.names_of(
        [], _dental_words(), titles=[("New Patients | Downtown Dental", "Downtown Dental Care", "downtowndental.com")]
    )
    assert {" ".join(f.words): f.proper for f in names.forms} == {
        "new patients downtown dental": False,  # it holds the brand
        "new patients downtown": False,
        "new patients": True,  # a tagline: a name only written as one
        "downtown dental": False,
        "downtown": True,
    }
    # the re-review's M-4: the word shared is one of four letters or more that no business here has in its name
    short = rm.names_of([], _dental_words(), titles=[("PA Family Dentistry", "Main Street Smiles", "pafamily.example")])
    assert {" ".join(f.words): f.proper for f in short.forms} == {"pa family dentistry": True, "pa family": True}
    hyphen = rm.names_of([], _dental_words(), titles=[("Gentle Giant Dental", "Kim Orthodontics", "gentle-giant.com")])
    assert {" ".join(f.words): f.proper for f in hyphen.forms}["gentle giant dental"] is False
    nowhere = rm.names_of([], _dental_words(), titles=[("Zenith Smiles | Family Dentist", "Old Mill Dental", None)])
    assert {" ".join(f.words): f.proper for f in nowhere.forms}["zenith smiles"] is True
    new = rm.names_of([], _dental_words(), titles=[("New Patients | New Smile Dental", "New Smile Dental", None)])
    assert {" ".join(f.words): f.proper for f in new.forms}["new patients"] is True  # "new" is shared, but short
    town = rm.names_of(
        [],
        _dental_words(),
        titles=[("Teeth Whitening Palo Alto | Bright Now! Dental", "Bright Now! Dental", "paloalto")],
    )
    assert {" ".join(f.words): f.proper for f in town.forms}["teeth whitening palo alto"] is True  # a town's label


@pytest.mark.parametrize(
    "text, search, dropped",
    [
        ("new patients dentist palo alto", True, None),  # "New Patients | New Smile Dental": "new" is too short
        ("teeth whitening palo alto", True, None),  # paloalto.brightnow.com: a host named for the town
        ("new smile dental palo alto", True, researcher.NAMED),
        ("bright now dental", True, researcher.NAMED),
    ],
)
def test_a_title_shares_its_brand_only_by_a_word_no_business_here_has_and_long_enough(
    conn, monkeypatch, text, search, dropped
):
    """The re-review's M-4: a part of a title is the brand only by a word of four letters or more that no business of
    the trade here has in its name (not the town's, the trade's, a service's or a stopword), in the Places name or in
    the host's first label -- never by a short word ("New") or a label named for the town (paloalto.brightnow.com)."""
    knowledge = {**knowledge_fixture().model_dump(), "name": "Hillside Dental"}
    rivals = ("New Smile Dental", "Bright Now! Dental & Orthodontics")
    g = _gathered(conn, monkeypatch, knowledge, rivals, {1: NEW_SMILE, 2: BRIGHT_NOW})
    assert _dropped(g, text, search=search) == dropped


# The re-review's N1: a one-word name counts written with a capital wherever it is in a sentence -- first, after "(",
# ":", a quote or "Dr." -- and is masked so wherever a run keeps it; a word any business here has is still no name
ONE_WORD = ("Zenith", "Sonrisa Dental", "Smith Dental", "Emergency", "微笑牙科")


@pytest.mark.parametrize(
    "text, dropped",
    [
        ("Zenith is open on Saturdays.", researcher.NAMED),
        ("Sonrisa offers free exams.", researcher.NAMED),  # the one word of "Sonrisa Dental"
        ("Patients like Dr. Smith for implants.", researcher.NAMED),  # after "Dr."
        ('Patients call it "Zenith" here.', researcher.NAMED),  # after a quote
        ("Open late: Sonrisa and two others.", researcher.NAMED),  # after a colon
        ("Most nearby practices (Zenith among them) open early.", researcher.NAMED),  # after "("
        ("Emergency appointments are rare nearby.", None),  # "Emergency": a word any business here has
        ("Patients like a zenith of comfort.", None),  # not written as a name
        ("微笑牙科 offers free exams.", researcher.NAMED),  # a script without capitals: its name counts as written
    ],
)
def test_a_one_word_name_counts_written_with_a_capital_wherever_it_is_in_a_sentence(conn, monkeypatch, text, dropped):
    knowledge = {**knowledge_fixture().model_dump(), "name": "Hillside Dental"}
    g = _gathered(conn, monkeypatch, knowledge, ONE_WORD)
    assert _dropped(g, text, search=False) == dropped
    if dropped:
        stored, _ = researcher._guarded(_brief(findings=[_finding(text)]), g)
        masked = researcher._stored(stored, g.names, g.hosts)["findings"][0]["text"]
        assert MASK in masked and not {"zenith", "sonrisa", "smith", "微笑牙科"} & set(rm.words(masked)), masked


# The re-review's M-1: a store number is no part of a name
@pytest.mark.parametrize(
    "text, search",
    [
        ("Aspen Dental offers free exams.", False),
        ("aspen dental palo alto", True),
        ("Western Dental & Orthodontics offers free exams.", False),
        ("western dental and orthodontics palo alto", True),
    ],
)
def test_a_name_with_a_store_number_is_named_without_it(conn, monkeypatch, text, search):
    knowledge = {**knowledge_fixture().model_dump(), "name": "Hillside Dental"}
    g = _gathered(conn, monkeypatch, knowledge, ("Aspen Dental #4512", "Western Dental & Orthodontics #12"))
    assert _dropped(g, text, search=search) == researcher.NAMED


# The re-review's M-2: a person a name or a title names, wherever the honorific is, or by a doctor's suffix
@pytest.mark.parametrize(
    "text",
    [
        "Raj Patel offers free exams.",  # "Meet Dr. Raj Patel - University Dental Care": the honorific inside a part
        "Dr. Patel offers free exams.",
        "Dr. Doe offers free exams.",  # "Jane Doe, DDS": a doctor's suffix
        "Jane Doe offers free exams.",
        "Dr. Chen offers free exams.",  # "Robert Chen DDS, MS"
        "Robert Chen offers free exams.",
    ],
)
def test_a_person_is_named_by_an_honorific_anywhere_in_a_part_or_by_a_doctors_suffix(conn, monkeypatch, text):
    knowledge = {**knowledge_fixture().model_dump(), "name": "Hillside Dental"}
    rivals = ("University Dental Care", "Jane Doe, DDS", "Robert Chen DDS, MS")
    g = _gathered(conn, monkeypatch, knowledge, rivals, {1: UNIVERSITY})
    assert _dropped(g, text, search=False) == researcher.NAMED


def test_a_search_the_business_runs_is_kept_beside_a_rival_named_for_it(conn, monkeypatch):
    """The re-review's M-3: the words of the searches the business's plan runs and keeps out, and of its services'
    own searches, are its own: a rival named for one ("Dental Implants Palo Alto", "Same Day Crowns") is a description
    -- the search is kept, the name written as a name still is one."""
    services = [
        *knowledge_fixture().model_dump()["services"],
        {"name": "Restorations", "intent_phrases": ["same day crowns"], "price_hint": None},  # its own search's words
    ]
    knowledge = {**knowledge_fixture().model_dump(), "name": "Hillside Dental", "services": services}
    _gathered(conn, monkeypatch, knowledge, ("Dental Implants Palo Alto", "Same Day Crowns"))
    conn.execute(
        "UPDATE plan SET json = jsonb_set(json, '{sections,ads,channels,0,themes}', %s::jsonb)",
        (json.dumps(["dental implants palo alto"]),),
    )
    g = rm.gather(conn, config.load(), rf.BIZ, {})
    assert g.base["plan"]["searches"] == ["dental implants palo alto"]
    assert _dropped(g, "dental implants palo alto", search=True) is None
    assert _dropped(g, "same day crowns", search=True) is None
    assert _dropped(g, "Ask Dental Implants Palo Alto for a quote.", search=False) == researcher.NAMED


def test_what_is_stored_of_a_sentence_holds_no_distinctive_form(conn, monkeypatch):
    """Masking uses the forms the guard finds: what a run keeps of a dropped sentence or search has each replaced --
    however it is written there (the re-review's N1) -- and what it keeps as written holds none, wherever the sentence
    is (a finding, an offer angle, an unknown); the feedback the next round reads names no one."""
    g = _dentist(conn, monkeypatch)
    named = [t for t, search in NAMED_DENTAL if not search]
    kept = [t for t, search in KEPT_DENTAL if not search]
    rest = named[researcher.MAX_FINDINGS :] + kept
    brief = _brief(
        findings=[_finding(t) for t in named[: researcher.MAX_FINDINGS]],
        suggested_themes=[t for t, search in NAMED_DENTAL + KEPT_DENTAL if search],
        offer_angles=rest[: researcher.MAX_ANGLES],
        unknowns=rest[researcher.MAX_ANGLES :],
    )
    stored, feedback = researcher._guarded(brief, g)
    remembered = {
        "brief": researcher._stored(stored, g.names, g.hosts),
        "feedback": researcher._unnamed(feedback, g.names, g.hosts, strict=True),
    }
    b = remembered["brief"]
    items = [x for key in ("findings", "offer_angles", "unknowns") for x in b[key]]
    for text in [*(x["text"] for x in items), *(s["text"] for s in b["suggested_themes"]), *remembered["feedback"]]:
        assert rm.names_in(text, g.names) is None, text
        assert rm.names_in(text, g.names, search=True) is None, text  # no distinctive form, written any way
    words = set(rm.words(json.dumps(remembered, ensure_ascii=False)))
    for name in ("jane", "doe", "smith", "jones", "lee", "orthodontics", "group", "clinica", "perez", "pa"):
        assert name not in words, name
    assert b["findings"][1]["text"] == f"{MASK}'s office is open on Saturdays."
    assert [x["dropped"] for x in items] == [researcher.NAMED] * len(named) + [None] * len(kept)
    assert [x["text"] for x in items[len(named) :]] == kept
    assert "finding 2: it named a business nearby; describe it instead" in feedback  # the name is never written


def test_a_rival_whose_name_is_within_the_businesss_own_names_only_itself(conn, monkeypatch):
    """The business's own name holds no rival's: Bright Smile Dental (the dental fixture) beside a rival called Smile
    Dental -- the business's own name is kept, the rival's is not."""
    g = _gathered(conn, monkeypatch, knowledge_fixture().model_dump(), ("Smile Dental",))
    assert _dropped(g, "bright smile dental palo alto", search=True) is None
    assert _dropped(g, "Bright Smile Dental offers same-week appointments.", search=False) is None
    assert _dropped(g, "smile dental palo alto", search=True) == researcher.NAMED
    assert _dropped(g, "Bright Smile Dental and Smile Dental both open early.", search=False) == researcher.NAMED


def test_a_town_the_business_gives_only_as_its_location_is_still_no_business(conn, monkeypatch):
    """Places is searched "in" the town or, when no town can be read, the location the business gave (a bare city):
    the places of that location -- all but the street of an address -- are the area's words too."""
    knowledge = {**knowledge_fixture().model_dump(), "name": "Hillside Dental", "service_area": []}
    g = _gathered(conn, monkeypatch, knowledge, DENTAL)
    conn.execute("UPDATE plan SET json = jsonb_set(json, '{sections,consultation,business,location}', '\"Palo Alto\"')")
    g = rm.gather(conn, config.load(), rf.BIZ, {})
    assert g.base["area"]["town"] is None and g.base["area"]["location"] == "Palo Alto"
    assert _dropped(g, "Most practices in Palo Alto open on Saturdays.", search=False) is None
    assert _dropped(g, "bright smile dental group palo alto", search=True) == researcher.NAMED
    assert not g.vocabulary.is_area("university")  # the street of an address is no area


def test_words_are_read_with_accents_case_and_punctuation_aside():
    assert rm.words("Clínica Dental Pérez") == ("clinica", "dental", "perez")
    assert rm.words("Smith & Jones, D.D.S.") == ("smith", "and", "jones", "dds")
    assert rm.words("Jane Doe, D.D.S, and P.C. Smith") == ("jane", "doe", "dds", "and", "pc", "smith")
    assert rm.words("J. Smith A.B.C.Dental") == ("j", "smith", "abc", "dental")
    assert rm.words("Dr. Lee’s Orthodontics | Palo Alto") == ("dr", "lee", "orthodontics", "palo", "alto")
    assert rm.words("Doctor Doe and Jones' office") == ("dr", "doe", "and", "jones", "office")
    assert rm.words("SMILE-STUDIO, 24/7") == ("smile", "studio", "24", "7")
    assert rm.words("STRAßE") == ("strasse",)
    assert [rm.words(5), rm.words(None), rm.words("")] == [(), (), ()]


def _dental_words() -> rm.Vocabulary:
    """What any family dentist in Palo Alto may have in its name: the trade, its services, the town and the state."""
    return rm.Vocabulary.of(
        trade=["dentists", "family dentist"], services=["Invisalign", "Cleanings"], area=["Palo Alto", "CA"]
    )


def _plumbing_words() -> rm.Vocabulary:
    return rm.Vocabulary.of(trade=["plumbing", "plumber"], area=["Palo Alto", "CA"])


def _forms(name: str, vocabulary: rm.Vocabulary | None = None) -> dict[str, bool]:
    """Each form of `name` by its words, and whether it names a business only written as a proper noun."""
    return {" ".join(f.words): f.proper for f in rm.names_of([name], vocabulary or _dental_words()).forms}


def test_a_name_yields_its_whole_its_parts_and_a_persons_name():
    assert _forms("Palo Alto Family Dentistry - Dr. Jane Doe, DDS") == {
        "palo alto family dentistry dr jane doe dds": False,
        "palo alto family dentistry dr jane doe": False,  # its legal suffix taken off
        "palo alto family dentistry": True,  # every word a dentist here may have: a name only written as one
        "palo alto family": True,  # each word of the trade taken off in turn
        "dr jane doe": False,
        "jane doe": False,  # the person without the honorific
        "dr doe": False,  # the honorific with the last word
    }
    assert _forms("Dr. Lee's Orthodontics | Palo Alto") == {
        "dr lee orthodontics palo alto": False,
        "lee orthodontics palo alto": False,
        "dr lee orthodontics": False,
        "lee orthodontics": False,
        "dr lee": False,  # the possessive ends the person's name; "lee" alone is too short to look for
    }
    assert _forms("Smith & Jones Dental") == {"smith and jones dental": False, "smith and jones": False}
    assert _forms("Bright Smile Dental Group of Palo Alto") == {
        "bright smile dental group of palo alto": False,
        "bright smile dental group": False,  # never by taking off a word that is not the trade's
    }


def test_a_name_of_words_any_business_here_has_is_a_name_only_as_a_proper_noun_and_a_town_is_none():
    assert _forms("Emergency Plumber", _plumbing_words()) == {"emergency plumber": True}
    assert _forms("Palo Alto Plumbing", _plumbing_words()) == {"palo alto plumbing": True}  # not "palo alto": a town
    assert _forms("Acme Plumbing", _plumbing_words()) == {"acme plumbing": False, "acme": True}
    assert _forms("Smile") == {"smile": True}
    assert _forms("The Smile") == {"the smile": True}  # one word beside a stopword is one word
    for none in ("Palo Alto", "CA", "Jo", "Dental", "Family", "Dr.", "DDS", "", "  "):
        assert _forms(none) == {}, none


@pytest.mark.parametrize(
    "text, named",
    [
        ("Emergency Plumber offers free estimates.", "Emergency Plumber"),
        ("They called Emergency Plumber.", "Emergency Plumber"),
        ("Emergency plumbers are busy at night.", None),
        ("An emergency plumber is busy at night.", None),
        ("Emergency plumber visits peak at night.", None),
        ("Plumbing in Palo Alto is busy.", None),  # its one word that is not a town's is first in the sentence
        ("We like Acme.", "Acme"),
        ("Acme is open.", "Acme"),  # a one-word name counts first in a sentence too (the re-review's N1)
        ("acme plumbing", "acme plumbing"),
    ],
)
def test_a_proper_noun_is_read_by_the_capitals_a_sentence_would_not_give(text, named):
    names = rm.names_of(["Emergency Plumber", "Plumbing in Palo Alto", "Acme Plumbing"], _plumbing_words())
    assert rm.names_in(text, names) == named
    assert rm.names_in(text.lower(), names, search=True) == ("acme plumbing" if named == "acme plumbing" else None)


def test_a_name_is_masked_where_it_is_found_and_nowhere_else():
    names = rm.names_of(DENTAL, _dental_words())
    texts = {
        "Dr. Jane Doe offers free exams.": f"{MASK} offers free exams.",
        "Jane Doe’s office is open.": f"{MASK}’s office is open.",
        "Clínica Dental Pérez abre.": f"{MASK} abre.",
        "smith & jones dental, SMITH AND JONES": f"{MASK}, {MASK}",
        "Palo Alto Family Dentistry and palo alto family dentistry": f"{MASK} and palo alto family dentistry",
        "Most practices in Palo Alto promise a bright smile.": None,
        "": "",
    }
    for text, masked in texts.items():
        assert rm.mask_names(text, names) == (text if masked is None else masked), text
        assert rm.names_in(rm.mask_names(text, names), names) is None, text
    assert rm.mask_names("Dr. Jane Doe", rm.Names()) == "Dr. Jane Doe"


def test_the_names_a_run_looks_for_are_every_business_found_and_every_page_read(conn, monkeypatch):
    """Every business the one call found, and the title of each page read: a business is named by its Places name or
    by its page's (test_research_material: the ones not shown too). The business's own name is never a rival's."""
    g = _plumber(conn, monkeypatch)
    forms = {" ".join(f.words) for f in g.names.forms}
    assert {"acme rooter", "bay drain experts", "emergency plumber", "water heater repair"} <= forms
    assert g.names.own == (("summit", "plumbing"), ("summit",))
    words = g.vocabulary
    assert all(words.is_trade(w) for w in ("plumber", "plumbers", "plumbing", "heating", "roofing"))
    assert all(words.is_area(w) for w in ("palo", "alto", "ca")) and not words.is_area("university")  # not a street
    assert words.is_generic("drain") and words.is_generic("heater") and not words.is_generic("rooter")
