import pytest

from agent import consult, models
from agent.tests.fixtures import FakeGenerator, knowledge_fixture

KNOWLEDGE = knowledge_fixture().model_dump()


def _session():
    tree = consult.load_tree("dentists")
    return {
        "id": "cs_1",
        "tree": "dentists",
        "answers": {},
        "options": consult.build_options(tree, KNOWLEDGE),
        "known_facts": {},
    }


def _q(qid):
    return next(q for q in consult.load_tree("dentists")["questions"] if q["id"] == qid)


def _common_q(qid):
    """A question of _common.json (website, business_name, services_offered): not in a trade's own "questions"
    (`_q`), but in every tree's full list (`consult.tree_questions`)."""
    return next(q for q in consult.tree_questions(consult.load_tree("dentists")) if q["id"] == qid)


@pytest.fixture
def session():
    return _session()


@pytest.fixture
def address_question():
    """Where the ads centre (walk fixes 1, Task 4): no options, words only."""
    return _q("location")


@pytest.fixture
def fake():
    return FakeGenerator()


def test_a_typed_place_is_kept_as_a_note_for_the_lookup(session, address_question, fake):
    fake.reads["10 miles, Houston TX"] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=10, note="Houston, TX", understood=True, services=[]
    )
    stored, why = consult.parse(session, address_question, {"other": "10 miles, Houston TX"}, fake)
    assert stored == {"note": "Houston, TX", "raw": "10 miles, Houston TX", "source": "other"} and why is None
    read = fake.read_calls[-1]  # the reader is told it reads an address, and offered no options
    assert (read["question"]["input"], read["options"]) == ("address", [])


def test_words_that_name_no_place_skip_the_question_at_once(session, address_question, fake):
    fake.reads["later please"] = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note=None, understood=False, services=[]
    )
    stored, why = consult.parse(session, address_question, {"other": "later please"}, fake)
    assert stored == {"skipped": True, "raw": "later please", "source": "other"} and why is None


def test_a_choice_for_the_address_question_is_a_clarification(session, address_question):
    stored, why = consult.parse(session, address_question, {"choice": "r5"}, None)
    assert stored is None and why == "Answer in your own words: an address, or a city and ZIP code."


def test_blank_words_for_the_address_question_ask_for_a_place(session, address_question, fake):
    stored, why = consult.parse(session, address_question, {"other": "   "}, fake)
    assert stored is None and why == "Send a few words: an address, or a city and ZIP code."
    assert fake.read_calls == []  # nothing to read


def test_a_choice_must_be_one_of_the_frozen_options():
    s = _session()
    ok, why = consult.parse(s, _q("objective"), {"choice": "bookings"}, gen=None)
    assert ok == {"choice": "bookings", "raw": "bookings", "source": "choice"} and why is None
    bad, why = consult.parse(s, _q("objective"), {"choice": "nope"}, gen=None)
    assert bad is None and "one of" in why


def test_more_than_three_lead_services_is_a_clarification_not_an_answer():
    s = _session()
    q = _q("service")
    four = ["svc_invisalign", "svc_cleanings", "svc_checkups", "svc_implants"]
    assert set(four) <= {o["id"] for o in s["options"]["service"]}  # every pick is one of the frozen options
    stored, why = consult.parse(s, q, {"choice": four}, gen=None)
    assert stored is None and why == consult.TOO_MANY_PICKS
    stored, why = consult.parse(s, q, {"choice": four[:3]}, gen=None)
    assert stored["choice"] == four[:3] and why is None
    a, b, c = four[:3]
    stored, why = consult.parse(s, q, {"choice": [a, a, b, c]}, gen=None)  # a repeated id counts once
    assert why is None and stored["choice"] == [a, b, c]


def test_free_text_goes_to_the_model_and_maps_to_an_option_or_a_number():
    gen = FakeGenerator(
        reads={
            "around three thousand": models.AnswerRead(
                option_id="1000_plus", amount_usd=3000, miles=None, note=None, understood=True, services=[]
            ),
            "we mostly want the phone to ring": models.AnswerRead(
                option_id="calls", amount_usd=None, miles=None, note=None, understood=True, services=[]
            ),
        }
    )
    s = _session()
    ok, _ = consult.parse(s, _q("customer_value"), {"other": "around three thousand"}, gen=gen)
    assert ok == {"choice": "1000_plus", "amount_usd": 3000.0, "raw": "around three thousand", "source": "other"}
    ok, _ = consult.parse(s, _q("objective"), {"other": "we mostly want the phone to ring"}, gen=gen)
    assert ok["choice"] == "calls" and gen.read_calls[-1]["question"]["id"] == "objective"


def test_unreadable_text_asks_once_then_is_kept_as_a_note():
    gen = FakeGenerator()
    s = _session()
    none, why = consult.parse(s, _q("service"), {"other": "zzz"}, gen=gen)
    assert none is None and why.endswith("?")
    s["answers"]["_retry"] = {"service": 1}
    kept, why = consult.parse(s, _q("service"), {"other": "zzz"}, gen=gen)
    assert kept == {"note": "zzz", "raw": "zzz", "source": "other"} and why is None


def test_other_text_is_bounded():
    s = _session()
    none, why = consult.parse(s, _q("service"), {"other": "x" * 501}, gen=FakeGenerator())
    assert none is None and "500" in why


def test_a_request_for_a_person_is_raised_when_support_is_on_and_read_as_before_when_off():
    import pytest

    words = "can someone just call me"
    gen = FakeGenerator(
        reads={
            words: models.AnswerRead(
                option_id=None,
                amount_usd=None,
                miles=None,
                note=None,
                understood=False,
                support="human",
                support_summary="Wants a call.",
                services=[],
            )
        }
    )
    with pytest.raises(consult.SupportAsk) as asked:
        consult.parse(_session(), _q("objective"), {"other": words}, gen, support=True)
    assert (asked.value.kind, asked.value.summary, asked.value.text) == ("human", "Wants a call.", words)
    stored, why = consult.parse(_session(), _q("objective"), {"other": words}, gen)
    assert stored is None and why.endswith("?")  # support off: the same words are an unreadable answer, as today


def _not_mine(**fields) -> models.AnswerRead:
    """A read of words that say the Google Maps place linked is not the owner's (plan
    2026-10-03-agent-listing-not-mine-words, D1)."""
    blank = {"option_id": None, "amount_usd": None, "miles": None, "note": None, "understood": False, "services": []}
    return models.AnswerRead(**{**blank, "listing_not_mine": True, **fields})


@pytest.mark.parametrize("qid", ["objective", "location"])
def test_words_that_say_the_listing_is_not_theirs_are_raised_only_for_a_linked_business(qid):
    """The model reads, the server decides (D2): with a listing the owner confirmed (`linked`) the words answer nothing
    -- ListingNotMine carries them -- and without one they take their usual path, the read's other fields."""
    words = "that's not my business on the map"
    gen = FakeGenerator(reads={words: _not_mine(option_id="calls", note="Houston, TX", understood=True)})
    with pytest.raises(consult.ListingNotMine) as raised:
        consult.parse(_session(), _q(qid), {"other": words}, gen, linked=True)
    assert raised.value.text == words
    stored, why = consult.parse(_session(), _q(qid), {"other": words}, gen)  # nothing linked: as any answer
    assert why is None and (stored.get("choice") == "calls" if qid == "objective" else stored["note"] == "Houston, TX")


def test_a_request_for_a_person_is_read_before_the_listing():
    """The support check stays first: words that ask for a person and say the place is not theirs open a ticket."""
    words = "not my business, get me a person"
    gen = FakeGenerator(reads={words: _not_mine(support="human", support_summary="Wants a person.")})
    with pytest.raises(consult.SupportAsk):
        consult.parse(_session(), _q("objective"), {"other": words}, gen, support=True, linked=True)


def test_a_complaint_alone_never_raises_the_listing():
    """Review focus 1: "this plan is not what I wanted" is dislike, not a listing -- a read that sets only `support`."""
    words = "this is not what I wanted"
    read = models.AnswerRead(
        option_id=None, amount_usd=None, miles=None, note=None, understood=False, support="dissatisfied", services=[]
    )
    gen = FakeGenerator(reads={words: read})
    stored, why = consult.parse(_session(), _q("objective"), {"other": words}, gen, linked=True)
    assert stored is None and why.endswith("?")


# ---------------------------------------------------------------- the name and the services (A3p-5 batch 1 Task 3)


def test_the_name_is_stored_as_typed_clipped_at_120_and_never_reaches_the_model(fake):
    """Q11: the words, stripped, are the name -- no model call. `raw` keeps the words as sent (ruling R3-a), even
    past the 120-character clip, so a correction's "Updated: …" can name what now stands."""
    q = _common_q("business_name")
    stored, why = consult.parse(_session(), q, {"other": "  Bright Smile Dental  "}, fake)
    assert stored == {"name": "Bright Smile Dental", "raw": "Bright Smile Dental", "source": "other"} and why is None
    long_name = "B" * 150
    stored2, why2 = consult.parse(_session(), q, {"other": long_name}, fake)
    assert stored2 == {"name": "B" * 120, "raw": long_name, "source": "other"} and why2 is None
    assert fake.read_calls == []


@pytest.mark.parametrize("words", ["skip", "Skip", "rather not say", "I'd rather not say", "No Name"])
def test_the_name_question_reads_skip_words_without_the_model(fake, words):
    """Q11: the owner's own ways of saying there is no name (after profile.norm) are a skip, kept as said."""
    stored, why = consult.parse(_session(), _common_q("business_name"), {"other": words}, fake)
    assert stored == {"skipped": True, "raw": words, "source": "other"} and why is None
    assert fake.read_calls == []


# Ruling F5: ways of saying "I would rather not say" (W4), each the whole answer -- never a word inside a longer one
SKIP_WORDS = [
    "skip",
    "Pass",
    "none",
    "N/A",
    "na",
    "No name",
    "We don't have a name yet",
    "I would rather not say",
    "I'd rather not say",
    "Rather not say",
    "Prefer not to say",
    "I'd prefer not to share",
    "I would rather not tell",
    "Prefer not to say, thanks",
    "skip please",
]


@pytest.mark.parametrize("words", SKIP_WORDS)
@pytest.mark.parametrize("qid", ["business_name", "services_offered"])
def test_skip_words_skip_the_name_or_the_services_without_the_model(fake, qid, words):
    """Ruling F5: the whole answer one way of saying the owner would rather not say skips either question, before any
    lookup or model call: nothing is read and nothing stands for a name or a service."""
    stored, why = consult.parse(_session(), _common_q(qid), {"other": words}, fake)
    assert (stored, why) == ({"skipped": True, "raw": words, "source": "other"}, None)
    assert fake.read_calls == []


@pytest.mark.parametrize("words", ["Skip's Plumbing", "Pass Creek Dental", "NA Roofing", "None Such Bakery"])
def test_a_name_that_holds_a_skip_word_is_a_name(fake, words):
    """Ruling F5: never a match inside a longer answer -- "Skip's Plumbing" is a real name."""
    stored, why = consult.parse(_session(), _common_q("business_name"), {"other": words}, fake)
    assert (stored, why) == ({"name": words, "raw": words, "source": "other"}, None)


def test_the_name_question_is_bounded_like_any_free_text(fake):
    """The generic length rules still apply before the name-specific ones."""
    q = _common_q("business_name")
    none, why = consult.parse(_session(), q, {"other": "   "}, fake)
    assert none is None and why == "Send a choice or a few words."
    none2, why2 = consult.parse(_session(), q, {"other": "x" * 501}, fake)
    assert none2 is None and "500" in why2
    assert fake.read_calls == []


def test_the_services_words_go_to_the_model_and_become_names():
    """Q12: the model's distinct services become the stored answer; the raw words are kept too."""
    text = "cleanings and Invisalign"
    gen = FakeGenerator(
        reads={
            text: models.AnswerRead(
                option_id=None,
                amount_usd=None,
                miles=None,
                note=None,
                understood=True,
                services=["Cleanings", "Invisalign"],
            )
        }
    )
    stored, why = consult.parse(_session(), _common_q("services_offered"), {"other": text}, gen)
    assert stored == {"services": ["Cleanings", "Invisalign"], "raw": text, "source": "other"} and why is None
    assert gen.read_calls[-1]["question"]["id"] == "services_offered"


def test_an_empty_services_read_asks_once_then_is_kept_as_a_note(fake):
    """Q12: the retry rule every free-text question follows -- an empty read is not understood the first time, and a
    note the second."""
    s = _session()
    q = _common_q("services_offered")
    none, why = consult.parse(s, q, {"other": "mumble mumble"}, fake)
    assert none is None and why == "I did not quite get that. In a few words, what should the answer be?"
    assert fake.read_calls  # the model was asked
    s["answers"][consult.RETRY_KEY] = {"services_offered": 1}
    kept, why2 = consult.parse(s, q, {"other": "mumble mumble"}, fake)
    assert kept == {"note": "mumble mumble", "raw": "mumble mumble", "source": "other"} and why2 is None


# ---------------------------------------------------------------- the website question asked again (task-2, R1, R2)


def _session_with(known: dict | None = None, answers: dict | None = None, options: dict | None = None) -> dict:
    return {"known_facts": known or {}, "answers": answers or {}, "options": options or {}, "tree": "dentists"}


WEBSITE_Q = {"id": "website", "input": "website", "prompt": "x", "options": [{"id": "none", "label": "No website"}]}
NAME_Q = {"id": "business_name", "input": "text", "prompt": "What is the business called?", "options": []}


def test_retry_and_ask_instead_answer_with_the_site_tried():
    s = _session_with(
        known={"website": {"url": "https://acme.example", "read": False}},
        options={"website": list(consult.RETRY_OPTIONS)},
    )
    assert consult.parse(s, WEBSITE_Q, {"choice": "retry"}, None) == (
        {"url": "https://acme.example", "retry": True, "raw": "retry", "source": "choice"},
        None,
    )
    assert consult.parse(s, WEBSITE_Q, {"choice": "ask_instead"}, None) == (
        {"url": "https://acme.example", "read": False, "asked": True, "raw": "ask_instead", "source": "choice"},
        None,
    )


@pytest.mark.parametrize(
    ("words", "answer"),
    [
        ("Try again", {"url": "https://acme.example", "retry": True, "raw": "Try again", "source": "other"}),
        ("try again!", {"url": "https://acme.example", "retry": True, "raw": "try again!", "source": "other"}),
        (
            "Ask me instead.",
            {"url": "https://acme.example", "read": False, "asked": True, "raw": "Ask me instead.", "source": "other"},
        ),
    ],
)
def test_the_label_of_try_again_or_ask_me_instead_typed_is_that_choice(words, answer):
    """Task 3 review (ruling I1a): while a site stands unopened, words that are an option's label -- "Try again", "Ask
    me instead", as profile.norm writes both -- are that option, as the site the reading saw is picked by its label:
    an owner, or the home's harness, who types the button's words never ends the question as no website, even with
    a retry counted (a miss on the re-asked question). Without a site unopened the words are no address."""
    s = _session_with(
        known={"website": {"url": "https://acme.example", "read": False}},
        options={"website": list(consult.RETRY_OPTIONS)},
    )
    assert consult.parse(s, WEBSITE_Q, {"other": words}, None) == (answer, None)
    s["answers"][consult.RETRY_KEY] = {"website": 1}
    assert consult.parse(s, WEBSITE_Q, {"other": words}, None) == (answer, None)
    assert consult.parse(_session_with(), WEBSITE_Q, {"other": words}, None) == (None, consult.NOT_A_SITE)
    assert consult.may_read_a_site(words) is (answer.get("retry") is True)  # Try again reads: counted (ruling I1a)


@pytest.mark.parametrize(
    ("words", "choice"),
    [
        ("Try again", consult.RETRY),
        ("try again please", consult.RETRY),
        ("ok try again", consult.RETRY),
        ("OK, try again. Thanks!", consult.RETRY),
        ("please retry", consult.RETRY),
        ("Retry", consult.RETRY),
        ("try it again", consult.RETRY),
        ("again", consult.RETRY),
        ("try once more", consult.RETRY),
        ("Ask me instead", consult.ASK_INSTEAD),
        ("ask me instead please", consult.ASK_INSTEAD),
        ("ask instead", consult.ASK_INSTEAD),
        ("ask me", consult.ASK_INSTEAD),
        ("okay, just ask me", consult.ASK_INSTEAD),
        ("try again tomorrow", None),
        ("don't try again", None),
        ("ask me later", None),
        ("please", None),
        ("", None),
    ],
)
def test_retry_typed_takes_the_buttons_words_courtesy_words_aside(words, choice):
    """Final review Important 2 (ruling ii): what an owner types for Try again or Ask me instead -- the label, a few
    other ways of saying it, with "please", "thanks" or "ok" at either end -- is that option; anything more is not."""
    assert consult.retry_typed(words) == choice
    s = _session_with(
        known={"website": {"url": "https://acme.example", "read": False, "tries": 1}},
        options={"website": list(consult.RETRY_OPTIONS)},
        answers={consult.RETRY_KEY: {"website": 1}},  # even on the re-asked question's second miss
    )
    stored, why = consult.parse(s, WEBSITE_Q, {"other": words or "x"}, None)
    tried = {"url": "https://acme.example", "raw": words, "source": "other"}
    if choice == consult.RETRY:
        assert (stored, why) == ({**tried, "retry": True}, None)
    elif choice == consult.ASK_INSTEAD:
        assert (stored, why) == ({**tried, "read": False, "asked": True}, None)
    else:  # no button's words: the retry rule's second miss, no address
        assert "url" not in stored and stored.get("none") is True and why is None


def test_retry_without_a_tried_site_asks_for_the_address():
    """Review focus 2: a stale card or an assistant sending the choice first -- nothing to retry, so the address is asked."""
    s = _session_with(options={"website": list(consult.RETRY_OPTIONS)})
    assert consult.parse(s, WEBSITE_Q, {"choice": "retry"}, None) == (None, consult.NOT_A_SITE)


def test_site_tries_left_counts_the_host_tried():
    s = _session_with(known={"website": {"url": "https://acme.example", "read": False}})
    assert consult.site_tries_left(s, "https://www.acme.example/") == 1  # start's own try counts as one
    s = _session_with(known={"website": {"url": "https://acme.example", "read": False, "tries": 2}})
    assert consult.site_tries_left(s, "https://acme.example") == 0
    assert consult.site_tries_left(s, "https://other.example") == 2


def test_site_reanswer_finds_an_address_while_the_site_is_unread():
    s = _session_with()
    assert (
        consult.site_reanswer(s, "https://acme.example/lp works fine for me. Why cannot you open it")
        == "https://acme.example/lp"
    )
    assert consult.site_reanswer(s, "we fix roofs") is None
    assert consult.site_reanswer(s, "see facebook.com/acme") is None  # a social page is no site of the business's
    read = _session_with(
        answers={"website": {"url": "https://acme.example", "read": True, "source": "other", "raw": "x"}}
    )
    assert consult.site_reanswer(read, "try https://acme.example") is None
    none = _session_with(answers={"website": {"none": True, "choice": "none", "raw": "none", "source": "choice"}})
    assert consult.site_reanswer(none, "try https://acme.example") is None  # No website said: the owner's word stands
    spent = _session_with(
        known={"website": {"url": "https://acme.example", "read": False, "tries": 2}},
        answers={"website": {"url": "https://acme.example", "read": False, "source": "other", "raw": "x"}},
    )
    assert consult.site_reanswer(spent, "https://acme.example again") is None
    assert consult.site_reanswer(spent, "or https://acme2.example") == "https://acme2.example"


@pytest.mark.parametrize(
    "ending",
    [
        {"note": "I'm not sure", "none": True, "raw": "I'm not sure", "source": "other"},
        {"social": "https://facebook.com/acme", "none": True, "raw": "facebook.com/acme", "source": "other"},
    ],
    ids=["a second miss's note", "a second social page"],
)
def test_site_reanswer_reads_past_the_retry_rules_own_endings(ending):
    """Final review Important 1 (ruling i): only the owner's own "No website" (`no_website_said`) stops the reroute --
    never the retry rule's note or social ending, which answered the question as no website with an address tried and
    a fetch left; the owner's "No website", tapped or said, still stops it."""
    known = {"website": {"url": "https://acme.example", "read": False, "tries": 1}}
    closed = _session_with(known=known, answers={"website": ending})
    assert consult.site_reanswer(closed, "https://acme.example") == "https://acme.example"
    assert consult.site_reanswer(closed, "it is www.acme2.example") == "https://www.acme2.example"
    for said in (
        {"choice": "none", "none": True, "raw": "none", "source": "choice"},
        {"none": True, "raw": "We don't have one", "source": "other"},
    ):
        assert (
            consult.site_reanswer(_session_with(known=known, answers={"website": said}), "https://acme.example") is None
        )


@pytest.mark.parametrize(
    "words, sentence",
    [
        ("Marina Bay Roofing", False),
        ("Bright Smile Dental of Palo Alto", False),
        ("Why cannot you open it?", True),
        ("https://acme.example works fine for me and I do not know why you cannot", True),
        ("Skip's Plumbing", False),
    ],
)
def test_looks_like_a_sentence(words, sentence):
    assert consult.looks_like_a_sentence(words) is sentence


def test_a_sentence_as_the_name_is_asked_again_once_then_kept():
    s = _session_with()
    assert consult.parse(s, NAME_Q, {"other": "why cannot you open it?"}, None) == (None, consult.NAME_AGAIN)
    s = _session_with(answers={consult.RETRY_KEY: {"business_name": 1}})
    stored, why = consult.parse(s, NAME_Q, {"other": "why cannot you open it?"}, None)
    assert (
        stored == {"name": "why cannot you open it?", "raw": "why cannot you open it?", "source": "other"}
        and why is None
    )


@pytest.mark.parametrize(
    "words",
    [
        "it's https://brightsmile.example, check again",
        "brightsmile.com",
        "Bright Smile Dental www.brightsmile.example",
    ],
)
def test_words_that_carry_a_web_address_are_no_name_asked_once_then_kept(words):
    """Final review Important 1 (ruling ii): a business's name carries no web address -- words that carry one
    (`site_in_words`, a social page passed over) are asked once more (NAME_AGAIN, the retry counted); the second time
    the words are the name (R0-l)."""
    assert consult.parse(_session_with(), NAME_Q, {"other": words}, None) == (None, consult.NAME_AGAIN)
    retried = _session_with(answers={consult.RETRY_KEY: {"business_name": 1}})
    assert consult.parse(retried, NAME_Q, {"other": words}, None) == (
        {"name": words, "raw": words, "source": "other"},
        None,
    )


@pytest.mark.parametrize(
    "words", ["St.Louis Roofing", "Skip's Plumbing", "Joe's (joe@acme.com)", "Acme on facebook.com"]
)
def test_a_name_with_no_web_address_of_its_own_is_the_name(words):
    """R2-f: a dot between words is no address by its shape alone; an e-mail address or a social page is no site."""
    assert consult.parse(_session_with(), NAME_Q, {"other": words}, None) == (
        {"name": words, "raw": words, "source": "other"},
        None,
    )


def test_build_options_offers_try_again_and_ask_instead_for_a_site_start_could_not_open():
    tree = consult.load_tree("dentists")
    opts = consult.build_options(tree, {}, known_facts={"website": {"url": "https://acme.example", "read": False}})
    assert opts["website"] == list(consult.RETRY_OPTIONS)
    assert consult.build_options(tree, {}, known_facts={})["website"] == [{"id": "none", "label": "No website"}]


def test_reading_view_and_say_name_what_the_site_gave():
    """R6 (spec 2026-10-12 §3.3): the host, the name only when the site vouches for it, up to six services, the
    areas, the phone, the platform's display name (site_install's own, never a client-side map) and the pages read --
    one sentence of what was found, or, with nothing but the host, the bare "I read {host}." alone."""
    data = {
        "website": "https://www.marinabayroofing.com/lp",
        "name": "Marina Bay Roofing",
        "name_source": "site",
        "services": [{"name": "Roof repair"}, {"name": "Roof replacement"}],
        "service_area": ["San Rafael", "Marin County"],
        "phone": "(628) 233-8140",
        "website_platform": "wordpress",
        "pages_read": 9,
    }
    view = consult.reading_view(data)
    assert view == {
        "host": "marinabayroofing.com",
        "name": "Marina Bay Roofing",
        "services": ["Roof repair", "Roof replacement"],
        "area": "San Rafael, Marin County",
        "phone": "(628) 233-8140",
        "platform": "WordPress",  # site_install.steps_for("wordpress")["name"]
        "pages": 9,
    }
    assert consult.reading_say(view) == (
        "I read marinabayroofing.com: Marina Bay Roofing; roof repair, roof replacement; "
        "San Rafael, Marin County; (628) 233-8140. Tell me if anything is off."
    )
    bare = consult.reading_view({"website": "https://acme.example", "name": "Acme", "name_source": "words"})
    assert bare["name"] is None and consult.reading_say(bare) == "I read acme.example."
    # a builder Admiry cannot name -- "other", one site_install does not know, none -- is no "Built on" row at all
    for platform in ("other", "some-new-builder", None):
        assert consult.reading_view({**data, "website_platform": platform})["platform"] is None, platform
