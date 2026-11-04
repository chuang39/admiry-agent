"""K5 (R-K5b, P10): a business on the dev box as a golden case, with what could identify it replaced -- the name
wherever it appears, phones, e-mail and web addresses, the street, the map point, the place id, the reviews."""

import hashlib
import json
import re
import time
import unicodedata

import pytest
from psycopg.types.json import Jsonb

from agent import business, models
from agent.eval import __main__ as cli
from agent.eval import anonymize, cases

REAL = {
    "name": "Kowalski Family Dental",
    "name_source": "owner",
    "category": "dentists",
    "category_detail": "family dentist (Dr. Kowalski)",
    "service_area": ["Chicago", "Evanston"],
    "stated_address": "1234 N Clark St",
    "services": [
        {
            "name": "Invisalign",
            "intent_phrases": ["invisalign chicago", "kowalski dental invisalign"],
            "price_hint": None,
        },
        {"name": "Cleanings", "intent_phrases": ["teeth cleaning chicago"], "price_hint": "$99, call 312-555-7788"},
    ],
    "hours": "Mon-Fri 8-5",
    "phone": "(312) 555-7788",
    "website": "https://kowalskidental.com",
    "pricing_hints": ["cleanings $99 (see kowalskidental.com/prices)"],
    "typical_customer_value_usd": 2500,
    "usp": ["Dr. Kowalski has 20 years of experience", "Email hello@kowalskidental.com for a same-week visit"],
    "reviews_summary": "Maria K. says Dr. Kowalski is wonderful",
    "compliance_flags": ["healthcare"],
    "locale": "en-US",
    "goal_hint": {"target_customers_per_month": 10, "monthly_budget_usd": 1500, "focus_services": ["Invisalign"]},
    "listing": {
        "place_id": "ChIJreal123",
        "name": "Kowalski Family Dental",
        "address": "1234 N Clark St, Chicago, IL 60610",
        "lat": 41.90345,
        "lng": -87.63123,
        "confirmed": True,
        "website": "https://kowalskidental.com",
        "phone": "(312) 555-7788",
    },
}
OPENING = (
    "Kowalski Family Dental in Chicago. Call 312-555-7788 or see www.kowalskidental.com. We want Invisalign patients."
)
ANSWERS = {
    "website": {"choice": "site_known", "url": "https://kowalskidental.com", "source": "choice"},
    "business_name": {"name": "Kowalski Family Dental", "source": "known_fact"},
    "listing": {"choice": "gm_1", "source": "choice"},
    "objective": {"choice": "bookings", "source": "choice"},
    "service": {"choice": ["svc_invisalign"], "source": "choice"},
    "customer_value": {"amount_usd": 3000, "raw": "3000", "source": "known_fact"},
    "radius": {"miles": 5, "raw": "5 miles", "source": "known_fact"},
}


def seed_real(conn, knowledge=REAL, opening=OPENING) -> None:
    conn.execute(
        "INSERT INTO business (id, owner_email, claim_state) VALUES ('biz_real', 'owner@kowalskidental.com', 'verified')"
    )
    business.add_knowledge(conn, "biz_real", knowledge, source="owner")
    business.set_goal(conn, "biz_real", monthly_limit_usd=1500)
    conn.execute(
        "INSERT INTO consult_session (id, business_id, tree, opening_text, answers, status, expires_at) "
        "VALUES ('cs_real', 'biz_real', 'dentists', %s, %s, 'planned', now() + interval '1 day')",
        (opening, Jsonb(ANSWERS)),
    )


def test_no_original_name_phone_email_or_url_survives_anywhere(conn, tmp_path):
    seed_real(conn)
    case = anonymize.validated(anonymize.case_of(conn, "biz_real", "dentists-6"))
    text = json.dumps(case).lower()
    for original in ("kowalski", "555-7788", "hello@", "1234 n clark", "chijreal123", "41.90345", "-87.63123", "maria"):
        assert original not in text, original
    k = case["knowledge"]
    assert k["name"] in anonymize.FAKE_NAMES["dentists"] and k["website"] is None and k["reviews_summary"] is None
    assert k["phone"].startswith("312-555-01") and k["listing"]["address"] == "100 Main St, Chicago, IL 60610"
    assert (k["listing"]["lat"], k["listing"]["lng"], k["listing"]["place_id"]) == (41.9, -87.63, "eval_dentists_6")
    assert case["answers"]["website"] == {"choice": "none"} and case["answers"]["business_name"] == {"other": k["name"]}
    assert case["answers"]["service"] == {"choice": "svc_invisalign"} and case["answers"]["radius"] == {"choice": "r5"}
    assert case["answers"]["customer_value"] == {"choice": "1000_plus"} and "listing" not in case["answers"]
    assert case["monthly_budget_usd"] == 1500 and case["plan"] is None
    path = tmp_path / "dentists-6.json"
    cases.write(path, case)
    assert cases.load(path).knowledge["category"] == "dentists"


def test_case_from_refuses_on_prod_a_business_without_a_confirmed_listing_and_a_wrong_id(conn, monkeypatch, capsys):
    seed_real(conn, {**REAL, "listing": {**REAL["listing"], "confirmed": False}})
    assert cli.main(["case-from", "biz_real", "--id", "dentists-6"]) == 1
    assert "confirmed Google listing" in capsys.readouterr().err
    assert cli.main(["case-from", "biz_real", "--id", "attorneys-9"]) == 1
    assert "dentists-<n>" in capsys.readouterr().err
    monkeypatch.setenv("AGENT_ENV", "prod")
    assert cli.main(["case-from", "biz_real", "--id", "dentists-6"]) == 1
    assert "refuses on prod" in capsys.readouterr().err
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'ops.eval_case'").fetchone()["n"] == 0


def test_case_from_prints_the_case_and_leaves_an_audit_row_with_no_business(conn, capsys):
    seed_real(conn)
    assert cli.main(["case-from", "biz_real", "--id", "dentists-6"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["id"] == "dentists-6" and "kowalski" not in json.dumps(printed).lower()
    row = conn.execute("SELECT business_id, args_redacted FROM agent_action WHERE tool = 'ops.eval_case'").fetchone()
    assert row["business_id"] is None and row["args_redacted"] == {"business": "biz_real", "case": "dentists-6"}


HVAC = {
    "name": "Zielinski Heating & Air",
    "name_source": "owner",
    "category": "home_services",
    "category_detail": "HVAC contractor, family owned (Zielinski since 1998)",
    "trade": "home_services/hvac",
    "service_area": ["Tampa", "Brandon"],
    "stated_address": "88 Palm Ave",
    "services": [
        {
            "id": "svc_zielinski_tune_up",
            "name": "Zielinski Tune-Up",
            "intent_phrases": ["zielinski tune up"],
            "price_hint": None,
        },
        {
            "id": "svc_ac_repair",
            "name": "AC repair",
            "intent_phrases": ["ac repair tampa"],
            "price_hint": "$89, 813 555 4321",
        },
    ],
    "hours": "Mon-Sat 7-7. Closed Sunday, see zielinskihvac.net/hours or visit 88 Palm Ave",
    "phone": "813 555 4321",
    "website": "http://www.zielinskihvac.net",
    "pricing_hints": ["$89 visit, by the Zielinski guarantee"],
    "typical_customer_value_usd": 900,
    "usp": ["Zielinski techs, 88 palm ave"],
    "reviews_summary": "Pat R. loved the Zielinski crew",
    "compliance_flags": [],
    "locale": "en-US",
    "goal_hint": {
        "target_customers_per_month": None,
        "monthly_budget_usd": None,
        "focus_services": ["Zielinski Tune-Up"],
    },
    "notes": ["The owner's mobile is 813 555 9999, Zielinski"],
    "offers": [{"text": "Zielinski spring special"}],
    "website_unread": "zielinskihvac.net",
    "phone_hours": "Zielinski after hours",
    "listing_rejected": [{"name": "Zielinski Roofing"}],
    "listing": {
        "place_id": "ChIJhvac456",
        "name": "Zielinski Heating & Air",
        "address": "88 Palm Ave, Tampa, FL 33606",
        "lat": 27.93811,
        "lng": -82.46702,
        "confirmed": True,
    },
}
HVAC_ANSWERS = {
    "website": {"none": True},
    "business_name": {"name": "Zielinski Heating & Air", "source": "known_fact"},
    "objective": {"note": "calls, ring 813 555 4321 or mail us@zielinskihvac.net, Zielinski", "source": "choice"},
    "service": {"choice": ["svc_zielinski_tune_up"], "source": "choice"},
    "customer_value": {"amount_usd": 900, "raw": "900", "source": "known_fact"},
    "location": {"address": "88 Palm Ave, Tampa", "lat": 27.93811, "lng": -82.46702, "source": "known_fact"},
    "radius": {"miles": 15, "raw": "15 miles", "source": "known_fact"},
}


def seed_hvac(conn) -> None:
    conn.execute(
        "INSERT INTO business (id, owner_email, claim_state) VALUES ('biz_hvac', 'z@zielinskihvac.net', 'verified')"
    )
    business.add_knowledge(conn, "biz_hvac", HVAC, source="owner")
    conn.execute(
        "INSERT INTO consult_session (id, business_id, tree, opening_text, answers, status, expires_at) "
        "VALUES ('cs_hvac', 'biz_hvac', 'home_services', %s, %s, 'planned', now() + interval '1 day')",
        ("Zielinski Heating & Air, 88 Palm Ave. Mail tom@zielinskihvac.net or call 813 555 4321.", Jsonb(HVAC_ANSWERS)),
    )


def test_a_home_services_business_loses_its_name_street_and_every_free_text_field_the_case_does_not_name(conn):
    seed_hvac(conn)
    case = anonymize.validated(anonymize.case_of(conn, "biz_hvac", "home_services-9"))
    text = json.dumps(case).lower()
    for original in (
        "zielinski",
        "88 palm",
        "4321",
        "9999",
        "chijhvac",
        "27.93811",
        "-82.46702",
        "pat r",
        "tom@",
        ".net",
    ):
        assert original not in text, original
    k = case["knowledge"]
    assert k["trade"] == "hvac" and k["category_detail"].startswith(
        "HVAC contractor"
    )  # a vocabulary word, not the text
    for dropped in (
        "notes",
        "offers",
        "website_unread",
        "phone_hours",
        "listing_rejected",
        "name_source",
        "stated_address",
    ):
        assert dropped not in k, dropped
    assert all(set(s) == {"name", "intent_phrases", "price_hint"} for s in k["services"])
    assert set(k["goal_hint"]) == {"target_customers_per_month", "monthly_budget_usd", "focus_services"}
    assert k["phone"].startswith("813-555-01") and "100 Main St" in k["hours"] and "100 Main St" in k["usp"][0]
    renamed = "svc_" + k["name"].split()[0].lower() + "_tune_up"  # the service the owner picked, followed through
    assert case["answers"]["service"] == {"choice": renamed} and k["services"][0]["name"].endswith("Tune-Up")
    assert case["answers"]["customer_value"] == {"choice": "300_1000"}
    assert case["answers"]["radius"] == {"other": "15 miles"} and "location" not in case["answers"]
    assert case["answers"]["website"] == {"choice": "none"} and case["monthly_budget_usd"] is None
    assert case["answers"]["objective"]["other"].startswith("calls, ring 813-555-01")


def test_a_trade_that_is_free_text_in_the_knowledge_is_only_ever_one_of_the_vocabulary_words(conn):
    seed_hvac(conn)
    business.add_knowledge(
        conn,
        "biz_hvac",
        {
            **HVAC,
            "trade": "Zielinski plumbing",
            "category_detail": "family",
            "services": [{**HVAC["services"][1], "name": "Help"}],
        },
        source="owner",
    )
    assert anonymize.case_of(conn, "biz_hvac", "home_services-9")["knowledge"]["trade"] == "plumbing"


def test_a_figure_field_that_holds_words_is_dropped_and_a_point_that_is_no_number_refuses(conn):
    seed_real(conn)
    wordy = {
        **REAL,
        "typical_customer_value_usd": "about 2500, Kowalski's average",
        "goal_hint": {"monthly_budget_usd": "1500 Kowalski"},
    }
    business.add_knowledge(conn, "biz_real", wordy, source="owner")
    case = anonymize.validated(anonymize.case_of(conn, "biz_real", "dentists-6"))
    assert case["knowledge"]["typical_customer_value_usd"] is None
    assert case["knowledge"]["goal_hint"] == {
        "target_customers_per_month": None,
        "monthly_budget_usd": None,
        "focus_services": [],
    }
    business.add_knowledge(
        conn, "biz_real", {**REAL, "listing": {**REAL["listing"], "lat": "Kowalski St"}}, source="owner"
    )
    with pytest.raises(cases.CaseError, match="not a pair of numbers") as raised:
        anonymize.case_of(conn, "biz_real", "dentists-6")
    assert "kowalski" not in str(raised.value).lower()


def test_every_field_of_business_knowledge_is_named_by_the_anonymizer():
    assert anonymize.HANDLED == set(
        models.Knowledge.model_fields
    )  # a new field is scrubbed or dropped in the same change


def test_a_case_takes_its_answers_from_a_consultation_of_its_own_trade(conn):
    seed_real(conn)
    conn.execute("UPDATE consult_session SET tree = 'fallback' WHERE id = 'cs_real'")
    with pytest.raises(cases.CaseError, match="no consultation"):
        anonymize.case_of(conn, "biz_real", "dentists-6")


# ---- the forms an identifying value comes in (review round 1): each is written into free text, the knowledge or the
# ---- address of a business whose other fields carry nothing of it, and must be gone from the printed JSON


def plain(text: str) -> str:
    """Lower case, accents and combining marks off, apostrophes straight: what a reader sees of a word."""
    kept = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return kept.lower().replace("’", "'")


def clean(**changes) -> dict:
    """A dentist whose knowledge says nothing of Kowalski, its street or its web address but what `changes` bring."""
    base = {
        **REAL,
        "category_detail": "family dentist",
        "services": [
            {"name": "Invisalign", "intent_phrases": ["invisalign chicago"], "price_hint": None},
            {"name": "Cleanings", "intent_phrases": ["teeth cleaning chicago"], "price_hint": None},
        ],
        "website": None,
        "pricing_hints": ["cleanings $99"],
        "usp": ["twenty years of experience"],
        "reviews_summary": None,
        "listing": {key: value for key, value in REAL["listing"].items() if key not in ("website", "phone")},
    }
    return {**base, **changes}


def printed(conn, knowledge: dict, opening: str = "We want Invisalign patients in Chicago.") -> tuple[dict, str]:
    seed_real(conn, knowledge, opening)
    case = anonymize.validated(anonymize.case_of(conn, "biz_real", "dentists-6"))
    return case, plain(json.dumps(case, ensure_ascii=False))


NAMES = [
    ("Kowalski's Family Dental", ["Dr. Kowalski sees you", "Kowalski’s own crew"], ["kowalski"]),
    ("Williams' Dental", ["Ask Dr. Williams", "Williams' crew"], ["williams"]),
    ("Smith-Jones Dental", ["Mr. Smith and Ms. Jones", "Smith-Jones since 1998"], ["smith", "jones"]),
    ("O’Brien Family Dental", ["Dr. O'Brien", "Dr. O’Brien"], ["brien"]),
    ("Nuñez Dental", ["Dr. Nuñez", "Dr. Nunez", "Dr. NUÑEZ"], ["nunez"]),
    ("Müller Dental", ["Dr. Müller", "Dr. Muller", "MÜLLER"], ["muller"]),
    ("Nguyễn Dental", ["Dr. Nguyễn", "Dr. Nguyen", "Dr. Nguyễn"], ["nguyen"]),
    ("李医生 Dental", ["李医生 Dental clinic", "Dr. 李医生"], ["李医生"]),
    (
        "Kowalski Family Dental",
        ["@kowalski_dental", "#KowalskiSmiles", "kowalskidental", "fb.me/KowalskiFamilyDental", "Kowalskis"],
        ["kowalski"],
    ),
    ("Kowalski Family Dental", ["DrKowalski", "McKowalski", "Call DrKowalski"], ["kowalski"]),
    (
        "Smith Jones Dental",
        ["#SmithJonesDental", "smithjones", "SmithAndJones", "smith-jones", "@SmithJones", "Smith&Jones"],
        ["smith", "jones"],
    ),
    (
        "Kowalski Nowak Dental",
        ["#KowalskiNowak", "kowalskinowakdental", "KowalskiNowak", "Nowak, Kowalski"],
        ["kowalski", "nowak"],
    ),
]


@pytest.mark.parametrize(("name", "texts", "gone"), NAMES)
def test_a_name_goes_in_every_form_it_is_written_in(conn, name, texts, gone):
    knowledge = clean(name=name, usp=texts, listing={**clean()["listing"], "name": name})
    case, text = printed(conn, knowledge, f"{name} in Chicago. Ask for Dr. {texts[0]}.")
    for word in gone:
        assert word not in text, word
    assert case["knowledge"]["name"] in anonymize.FAKE_NAMES["dentists"]


def test_a_name_word_is_replaced_once_and_what_replaced_it_is_never_replaced_again(conn):
    fake = anonymize.FAKE_NAMES["dentists"][int(hashlib.sha256(b"biz_real").hexdigest(), 16) % 4]
    stem = fake.split()[0][:5]  # a business named by the first letters of the name it is given
    case, _ = printed(conn, clean(name=f"{stem} Family Dental", usp=[f"{stem} Family Dental is kind", f"{stem}s"]))
    assert case["knowledge"]["usp"] == [f"{fake} is kind", f"{fake.split()[0]}s"]


PHONES = [
    "(312) 555-7788",
    "312-555-7788",
    "312.555.7788",
    "+13125557788",
    "13125557788",
    "3125557788",
    "+1 (312) 555-7788",
    "312/555-7788",
    "312 - 555 - 7788",
    "555-7788",
    "555 7788",
    # any dash a phone is typed or pasted with (review round 4)
    "312\N{NON-BREAKING HYPHEN}555\N{NON-BREAKING HYPHEN}7788",
    "312\N{EN DASH}555\N{EN DASH}7788",
    "312\N{EM DASH}555\N{EM DASH}7788",
    "312\N{MIDDLE DOT}555\N{MIDDLE DOT}7788",
    "312_555_7788",
    "(312) 555\N{NON-BREAKING HYPHEN}7788",
    "555\N{EN DASH}7788",
]


@pytest.mark.parametrize("form", PHONES)
def test_a_phone_goes_in_every_form_it_is_written_in(conn, form):
    case, text = printed(conn, clean(usp=[f"Call {form} today"]), f"Ring {form} or write.")
    assert "7788" not in text, form
    assert case["knowledge"]["usp"][0].startswith("Call ") and case["knowledge"]["usp"][0].endswith(" today")


@pytest.mark.parametrize(
    "form",
    [
        "+13125557788",
        "13125557788",
        "312.555.7788",
        "312/555/7788",
        "312 555 7788",
        "312\N{NON-BREAKING HYPHEN}555\N{NON-BREAKING HYPHEN}7788",
        "312\N{EN DASH}555\N{EN DASH}7788",
        "312\N{EM DASH}555\N{EM DASH}7788",
        "312\N{MIDDLE DOT}555\N{MIDDLE DOT}7788",
        "312_555_7788",
    ],
)
def test_the_area_code_of_a_phone_in_any_form_is_kept(conn, form):
    case, text = printed(conn, clean(phone=form))
    assert case["knowledge"]["phone"].startswith("312-555-01") and "7788" not in text


ADDRESSES = [
    "kowalskidental.clinic",
    "brightsmiles.clinic",
    "goo.gl/maps/AbC123",
    "g.page/r/CXyz123",
    "linktr.ee/brightsmiles",
    "https://linktr.ee/brightsmiles?x=1",
    "www.brightsmiles.tv",
    "@brightsmiles",
    "@kowalskidental",
    "hello [at] brightsmiles [dot] com",
    "hello (at) brightsmiles (dot) com",
    "brightsmiles [dot] com",
    "hello@brightsmiles.dental",
    "Dr.Kowalski@brightsmiles.com",
    # a profile link written as a handle, and a domain after dots that follow no word's last letter (review round 4)
    "@facebook.com/brightsmileschicago",
    "@instagram.com/brightsmiles",
    "online...brightsmiles.com",
    "online..brightsmiles.com/book",
    "see:.brightsmiles.com",
]


@pytest.mark.parametrize("form", ADDRESSES)
def test_a_web_address_e_mail_or_handle_goes_in_every_form_it_is_written_in(conn, form):
    _, text = printed(conn, clean(usp=[f"Find us at {form} soon"], website=None), f"Our page: {form}")
    for left in (
        "brightsmiles",
        "kowalski",
        "abc123",
        "cxyz123",
        "goo.gl",
        "g.page",
        "linktr",
        "[at]",
        "(at)",
        "[dot]",
    ):
        assert left not in text, (form, left)


# an address glued to what was matched just before it: the earlier match takes the address's left neighbour, and an
# address may start only at the start of a word
GLUED = [
    ("(312) 555-7788.drkowalski@gmail.com", ["7788", "drkowalski", "kowalski", "gmail"]),
    ("Call 312 555 7788.tom.z@gmail.com", ["7788", "312 555", "tom.z", "gmail"]),
    ("1234 N Clark St.tom.z@gmail.com", ["1234", "clark", "tom.z", "gmail"]),
    ("Kowalski Family Dental-drkowalski@gmail.com", ["kowalski", "gmail"]),
    ("(312) 555-7788.drkowalskidds.com", ["7788", "kowalski", "drkowalskidds"]),
    ("Kowalski Family Dental.kowalskidds.com", ["kowalski", "kowalskidds"]),
    ("Call 312-555-7788@brightsmiles", ["7788", "brightsmiles"]),
    ("tom@kowalski-family-dental.com", ["tom@", "kowalski", "dental.com"]),
    ("tom@kowalski.example.org", ["tom@", "kowalski", "example.org"]),
    ("(312) 555-7788.tom@kowalski-family-dental.com", ["7788", ".tom", "tom@", "kowalski", "dental.com"]),
    ("Kowalski Family Dental@brightsmiles.com", ["kowalski", "brightsmiles", "@"]),
    # review round 4: an [at]/(at) address whose domain the scan took on its own leaves its local part and the [at]
    ("(312) 555-7788-tomzielinski [at] gmail [dot] com", ["7788", "tomzielinski", "gmail", "[at]"]),
    ("Kowalski Family Dental-tomzielinski (at) gmail (dot) com", ["kowalski", "tomzielinski", "gmail", "(at)"]),
    ("(312) 555-7788tomzielinski[at]gmail[dot]com", ["7788", "tomzielinski", "gmail", "[at]"]),
    ("1234 N Clark St.tom.z [at] gmail [dot] com", ["1234", "clark", "tom.z", "gmail", "[at]"]),
    ("(312) 555-7788.tom.z [at] gmail [dot] com", ["7788", "tom.z", ".z", "gmail", "[at]"]),
    ("(312) 555-7788.info [dot] tom@brightsmiles", ["7788", "info", "tom@", "brightsmiles", "[dot]"]),
    # a domain after two or three dots, and a profile link glued to a phone or the name
    ("(312) 555-7788..brightsmiles.com", ["7788", "brightsmiles"]),
    ("(312) 555-7788...brightsmiles.com/book", ["7788", "brightsmiles", "/book"]),
    ("Kowalski Family Dental..brightsmiles.com", ["kowalski", "brightsmiles"]),
    ("Call 312-555-7788@facebook.com/brightsmiles", ["7788", "facebook", "brightsmiles"]),
    ("Kowalski Family Dental@instagram.com/brightsmiles", ["kowalski", "instagram", "brightsmiles"]),
]


@pytest.mark.parametrize(("form", "gone"), GLUED)
def test_an_address_glued_to_an_earlier_match_goes_with_it(conn, form, gone):
    _, text = printed(conn, clean(usp=[form, f"{form} again"], website=None), form)
    for left in gone:
        assert left not in text, (form, left)


UNNUMBERED = [
    ("One Kowalski Plaza, Chicago, IL 60610", "Chicago, IL 60610", ["kowalski", "plaza"]),
    ("One Kowalski Plaza, Chicago, IL 60610, USA", "Chicago, IL 60610", ["kowalski", "plaza"]),
    ("N Clark St & Division St, Chicago, IL 60610", "Chicago, IL 60610", ["clark", "division"]),
    ("PO Box 12, Chicago, IL 60610", "Chicago, IL 60610", ["po box"]),
    ("One Kowalski Plaza", None, ["kowalski", "plaza"]),
    ("PO Box 12", None, ["po box"]),
    ("Kowalski Plaza, 1234 N Clark St, Chicago, IL 60610", "100 Main St, Chicago, IL 60610", ["kowalski", "clark"]),
    ("1234 N Clark St, Suite 200, Chicago, IL 60610", "100 Main St, Chicago, IL 60610", ["suite", "200", "clark"]),
    ("100 Main St, Suite 200, Chicago, IL 60610", "100 Main St, Chicago, IL 60610", ["suite", "200"]),
    ("1234 N Clark St #5, Chicago, IL 60610, USA", "100 Main St, Chicago, IL 60610", ["#5", "clark", "usa"]),
    ("1234 N Clark St, Kowalski Medical Building, Chicago, IL 60610", "100 Main St, Chicago, IL 60610", ["kowalski"]),
    ("500 Main St, Springfield, IL 62701", "100 Main St, Springfield, IL 62701", ["500 main"]),
    ("12 Main Street, Springfield, IL 62701", "100 Main St, Springfield, IL 62701", ["12 main"]),
    ("100 Main St, Springfield, IL 62701", "100 Main St, Springfield, IL 62701", []),
]


@pytest.mark.parametrize(("address", "kept", "gone"), UNNUMBERED)
def test_an_address_keeps_only_the_street_that_is_replaced_and_the_town_state_and_zip(conn, address, kept, gone):
    knowledge = clean(listing={**clean()["listing"], "address": address}, stated_address=None)
    case, text = printed(conn, knowledge, "We want Invisalign patients.")
    assert case["knowledge"]["listing"]["address"] == kept
    for word in gone:
        assert word not in text, word


STREETS = [
    "We're at 1234 N. Clark Street",
    "1234 North Clark St",
    "Visit us at 1234 N Clark St.",
    "Find 1234 N. Clark",
    "Clark Street office",
    "on N. Clark St near the lake",
    "1234 N. Clark Street, Suite 200",
    "1234 N Clark Street Unit 5",
]


@pytest.mark.parametrize("words", STREETS)
def test_the_street_goes_from_free_text_however_it_is_written(conn, words):
    _, text = printed(conn, clean(usp=[words], hours=f"Open daily, {words}"), words)
    for left in ("1234", "clark", "suite", "unit 5"):
        assert left not in text, (words, left)


def test_a_house_number_that_is_also_an_area_code_does_not_spoil_the_phone_or_the_street(conn):
    listing = {**clean()["listing"], "address": "312 W Randolph St, Chicago, IL 60601"}
    knowledge = clean(listing=listing, stated_address=None, usp=["Call 312-555-7788 or visit 312 W Randolph St"])
    case, text = printed(conn, knowledge)
    assert case["knowledge"]["usp"] == [f"Call {case['knowledge']['phone']} or visit 100 Main St"]
    assert "randolph" not in text and "7788" not in text


def test_the_compliance_flags_are_a_closed_vocabulary_that_a_name_word_cannot_corrupt(conn):
    flags = ["healthcare", " Legal ", "financial", "kowalski special", "housing", "alcohol", "cannabis", 7]
    case, _ = printed(conn, clean(name="Smile Health Dental", compliance_flags=flags))
    assert case["knowledge"]["compliance_flags"] == [
        "healthcare",
        "legal",
        "financial",
        "housing",
        "alcohol",
        "cannabis",
    ]
    described = models.Knowledge.model_fields["compliance_flags"].description
    assert set(re.findall(r"'(\w+)'", described)) == set(anonymize.COMPLIANCE_FLAGS)  # the vocabulary models.py names


def test_a_neighbourhood_dropped_from_the_address_is_dropped_from_the_service_area_never_rewritten(conn):
    listing = {**clean()["listing"], "address": "1234 N Clark St, Lincoln Park, Chicago, IL 60614"}
    knowledge = clean(
        listing=listing, stated_address=None, service_area=["Lincoln Park", "Chicago", "LINCOLN  park", "Evanston"]
    )
    case, text = printed(conn, knowledge)
    assert case["knowledge"]["service_area"] == ["Chicago", "Evanston"]
    assert case["knowledge"]["listing"]["address"] == "100 Main St, Chicago, IL 60614" and "lincoln" not in text


def test_the_street_the_case_puts_in_is_never_scrubbed_again(conn):
    listing = {**clean()["listing"], "address": "500 Main St, Springfield, IL 62701"}
    case, _ = printed(conn, clean(listing=listing, stated_address=None, service_area=["Springfield"]))
    assert case["knowledge"]["listing"]["address"] == "100 Main St, Springfield, IL 62701"


LONG_RUNS = [
    "a" * 50_000,
    "1" * 50_000,
    "a." * 25_000,
    "a-" * 25_000,
    "a@" * 25_000,
    "a [at] " * 7_000,
    "a [dot" * 8_000,
    "Kowalski" * 6_000,
    "a.b" * 16_000,
    "1 " * 25_000,
    "1-" * 25_000,
    "x " * 25_000,
    "x" + " " * 50_000,
    "(" + " " * 50_000,
    "\t" * 50_000 + "x",
    "KowalskiAndNowak" * 3_000,
    "1234 N Clark St " * 3_000,
    "x." * 10_000 + "com",
    # review round 4: the separators a phone now takes, a domain after dots, an [at] with no run after, a handle's path
    "1_" * 25_000,
    "1\N{EN DASH}" * 25_000,
    "1" + "\N{EM DASH}" * 50_000,
    "..a" * 16_000,
    ".a" * 25_000,
    "a [at]" + " " * 50_000,
    "[at] " * 10_000,
    "x (at)" * 8_000,
    "@a/" * 16_000,
    "a@b/" * 12_000,
]


@pytest.mark.parametrize("text", LONG_RUNS, ids=[f"{t[:7]!r}x{len(t)}" for t in LONG_RUNS])
def test_a_long_run_of_text_is_scrubbed_in_well_under_a_second(text):
    scrub = anonymize._Scrub(
        ["Kowalski Family Dental"], ["Chicago"], ["1234 N Clark St", "Lincoln Park"], "Elm", "312-555-0101"
    )
    started = time.perf_counter()
    scrub(text)
    assert time.perf_counter() - started < 1.0
