"""The consultation trees' fit flags and audience (spec 2026-09-29-admiry-channels-design §6.2; plan
2026-09-29-agent-m0-channel-framework Task 5): project-type and visual services carry `meta_fit` (and the project
trades NewsBreak's volume is made of, `newsbreak_fit`), and home services is a homeowner audience. The allocator reads
them through `consult.service_flags`."""

from agent import consult
from agent.tests.fixtures import knowledge_fixture

TREES = ("home_services", "dentists", "attorneys", "fallback")
FLAGS = ("meta_fit", "newsbreak_fit")
BOTH = {"meta_fit", "newsbreak_fit"}


def _service_options(name: str) -> list[dict]:
    return next(q for q in consult.load_tree(name)["questions"] if q["id"] == "service")["options"]


def test_service_options_carry_the_fit_flags_as_booleans():
    for name in TREES:
        for q in consult.load_tree(name)["questions"]:
            for o in q["options"]:
                assert all(o.get(k, False) in (True, False) for k in FLAGS), (name, o)
                if q["id"] != "service":
                    assert not set(FLAGS) & set(o), (name, o)  # only a service can fit a channel
    flagged = {
        name: {
            o["label"]: {k for k in FLAGS if o.get(k)} for o in _service_options(name) if any(o.get(k) for k in FLAGS)
        }
        for name in TREES
    }
    assert flagged == {
        "home_services": {
            "Remodels": BOTH,
            "Roofing": BOTH,
            "Windows": BOTH,
            "Solar panels": BOTH,
            "HVAC replacement": BOTH,
            "Landscaping projects": BOTH,
        },
        "dentists": {"Invisalign": {"meta_fit"}, "Cosmetic dentistry": {"meta_fit"}},
        "attorneys": {},
        "fallback": {},
    }


LOCATION_PROMPT = (
    "Where should the ads centre? A street address, or the city and ZIP code. If you would rather say later, just say "
    "so."
)


def test_every_tree_asks_where_the_ads_centre_right_before_the_radius():
    """Walk fixes 1, Task 4: the owner names the place the radius is drawn around; the radius stays the last question
    (consult.facts reads its options there)."""
    for name in TREES:
        questions = consult.load_tree(name)["questions"]
        assert [q["id"] for q in questions][-2:] == ["location", "radius"], name
        q = questions[-2]
        assert (q["input"], q["known_fact"], q["options"], q["why"]) == ("address", "address", [], None), name
        assert q["prompt"] == LOCATION_PROMPT, name


def test_every_radius_prompt_names_the_place_with_the_trees_own_noun_by_default():
    defaults = {}
    for name in TREES:
        q = next(q for q in consult.load_tree(name)["questions"] if q["id"] == "radius")
        assert q["prompt"] == "How far around {place} should the ads reach?", name
        defaults[name] = q["place_default"]
    assert defaults == {
        "home_services": "your business",
        "dentists": "the clinic",
        "attorneys": "the office",
        "fallback": "your business",
    }


def test_only_home_services_is_a_homeowner_audience():
    assert consult.load_tree("home_services")["audience"] == "homeowners"
    assert all("audience" not in consult.load_tree(name) for name in TREES if name != "home_services")


def test_the_flags_of_the_services_the_owner_picked():
    tree = consult.load_tree("dentists")
    session = {"options": consult.build_options(tree, knowledge_fixture().model_dump()), "answers": {}}

    def pick(*ids: str) -> dict:
        return {**session, "answers": {"service": {"choice": list(ids), "source": "choice"}}}

    # the business's own "Invisalign" (its site lists it) reads the flags of the tree's Invisalign
    assert consult.service_flags(pick("svc_invisalign"), tree) == {"meta_fit"}
    assert consult.service_flags(pick("svc_cleanings"), tree) == set()
    assert consult.service_flags(pick("svc_cleanings", "svc_invisalign"), tree) == {"meta_fit"}  # any pick fits
    words = {**session, "answers": {"service": {"note": "veneers", "source": "other"}}}
    assert consult.service_flags(words, tree) == set() and consult.service_flags(session, tree) == set()
    home = consult.load_tree("home_services")
    roofer = consult.build_options(home, {"services": [{"name": "Roofing"}]})
    assert consult.service_flags({"options": roofer, "answers": {"service": {"choice": ["svc_roofing"]}}}, home) == BOTH
    # a session frozen before the trees had flags (its options carry none) reads them from the tree by label
    old = {"options": {"service": [{"id": "svc_remodel", "label": "Remodels"}]}}
    assert consult.service_flags({**old, "answers": {"service": {"choice": ["svc_remodel"]}}}, home) == BOTH
