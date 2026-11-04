"""S0 (founder's dev walk, 2026-10-07): paying is not the end of the setup. With money on the balance (or the owner's
own account live) and the latest plan still a draft, the setup card's step is `review` -- Admiry takes a second look at
the plan (one model call over the plan, the business and the money, beside the launch's own read-only check) and the
owner confirms -- and the confirmation approves the plan and launches it from the card. `done` only once a plan is
approved. Before this, the card ended on "Your ads start when you approve the plan." with nothing to click, and the
home's launch card kept offering to prepay."""

import shutil

import pytest

from agent import ads_mode, agreements, config, legal, llm, models, plans
from agent.tests.test_ads_run import live, sections  # noqa: F401 -- `live` is a pytest fixture used by name
from agent.tests.test_ads_run_channels import add_plan_by_hand
from agent.tests.test_google_ads_link import pending_link
from agent.tests.test_home_api import fake as home_fake  # noqa: F401 -- the dental walk's fake, a fixture used by name
from agent.tests.test_wallet import fund

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="drives setup.js / home.js with node")


def draft_sections(**kw) -> dict:
    """test_ads_run's sections with the consultation fields the plan's cards read too (plans.cards: what counts as a
    result, how long it runs), as `recommend` stores them."""
    secs = sections(**kw)
    secs["consultation"] |= {"conversions": plans.CONVERSIONS["bookings"], "duration_days": 30, "offer": None}
    return secs


def funded_prepaid(conn, cents: int = 126000) -> None:
    """A claimed business that chose prepaid ads run by Admiry, accepted both documents and paid once."""
    pending_link(conn)
    conn.execute("DELETE FROM ads_link")
    ads_mode.change(conn, "biz_1", "managed")
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None, document=legal.ADDENDUM)
    fund(conn, cents=cents)


def test_with_money_in_and_a_draft_plan_the_card_is_done_and_the_plan_waits_for_the_owners_yes(conn):
    from agent import setup

    funded_prepaid(conn)
    s = config.load()
    assert setup.state(conn, s, "biz_1")["step"] == "done"  # money in, no plan yet
    plan_id = add_plan_by_hand(conn, sections(), biz="biz_1", status="draft")
    st = setup.state(conn, s, "biz_1")
    assert (st["step"], st["plan_id"], st["plan_status"], st["balance_usd"]) == ("done", plan_id, "draft", 1260)
    conn.execute("UPDATE plan SET status = %s WHERE id = %s", (plans.APPROVED, plan_id))
    assert setup.state(conn, s, "biz_1")["plan_status"] == "approved"


# ---------------------------------------------------------------- the model's second look (llm.review_plan)
REVIEW = models.PlanReview(
    summary="Google Search ads for Invisalign within 5 miles of Bright Smile Dental, about $33 a day; your $1,260 "
    "covers about 38 days.",
    checks=[
        models.ReviewCheck(topic="services", ok=True, text="The ads name Invisalign, the service you chose."),
        models.ReviewCheck(topic="money", ok=False, text="Your balance covers about 38 days at this budget."),
    ],
    grounded=True,
    grades=[
        models.PlanGrade(dimension=d, grade=4, note="fine")
        for d in ("fit", "budget", "copy", "keywords", "estimate", "clarity")
    ],
    missing=[],
)


def test_review_topics_are_the_six_a_marketer_checks_before_launch():
    assert models.ReviewTopic.__args__ == ("services", "area", "money", "page", "consultation", "compliance")


def test_review_plan_posts_one_completion_with_the_review_schema_over_the_material():
    """One developer message (SYSTEM_REVIEW) and one user message holding the material between tags: the plan's
    cards, what Admiry knows (as a model reads it: profile.for_model), the money and the launch's own refusal."""
    from agent import profile
    from agent.tests.fixtures import knowledge_fixture
    from agent.tests.test_llm import body, completion, generator

    sent: list = []
    knowledge = knowledge_fixture().model_dump()
    material = {
        "cards": {"plan": {"goal": "More Invisalign patients within 5 miles of Bright Smile Dental"}},
        "rationale": {"why": "Invisalign searches are high intent"},
        "knowledge": knowledge,
        "money": {"balance_usd": 1260, "daily_usd": 33, "runway_days": 38},
        "blocked": None,
    }
    out = generator(completion(REVIEW.model_dump_json()), sent=sent).review_plan(material)
    assert out == REVIEW
    b = body(sent[0])
    assert b["response_format"]["json_schema"]["name"] == "PlanReview"
    assert b["messages"][0] == {"role": "developer", "content": llm.SYSTEM_REVIEW}
    user = b["messages"][1]["content"]
    assert user.startswith("<material>") and "Bright Smile Dental" in user and '"runway_days": 38' in user
    assert llm._tag("material", {**material, "knowledge": profile.for_model(knowledge)}) == user
    for rule in ("Never state a number the material does not contain", "never blocks", "No advertising jargon"):
        assert rule in " ".join(llm.SYSTEM_REVIEW.split()), rule


# ---------------------------------------------------------------- plan_review.review: the second look, once per version
OWNER = {"email": "owner@example.com", "ip": "203.0.113.5"}


@pytest.fixture
def fake(monkeypatch):
    """The model faked for the second look alone (test_home_api's `fake`, imported below for the home's walk, answers
    the whole walk and a default review)."""
    from agent.tests.fixtures import FakeGenerator

    gen = FakeGenerator(reviews=[REVIEW])
    monkeypatch.setattr(llm, "generator", lambda settings=None: gen)
    return gen


def test_the_review_reads_the_plan_the_money_and_the_launch_check_once_per_version(conn, fake):
    from agent import plan_review

    funded_prepaid(conn)
    plan_id = add_plan_by_hand(conn, draft_sections(), biz="biz_1", status="draft")  # $1,000 a month: $33 a day
    out = plan_review.review(conn, config.load(), "biz_1", by=plan_review.By.owner(**OWNER))
    assert out["plan_id"] == plan_id and out["blocked"] is None and out["ask"] is None
    assert out["money"] == {"balance_usd": 1260, "daily_usd": 33, "runway_days": 38}
    assert out["summary"] == REVIEW.summary and out["grounded"] is True and out["model"] == "ok"
    assert out["checks"] == [c.model_dump() for c in REVIEW.checks]
    (call,) = fake.review_calls
    m = call["material"]
    assert m["money"] == out["money"] and m["blocked"] is None
    assert m["cards"]["plan"]["goal"].startswith("More ") and "rationale" in m and "knowledge" in m
    # once per version and balance: a second look at the same plan reads the stored one
    again = plan_review.review(conn, config.load(), "biz_1", by=plan_review.By.owner(**OWNER))
    assert again["summary"] == out["summary"] and len(fake.review_calls) == 1
    stored = conn.execute("SELECT plan_id, balance_cents, blocked, model FROM plan_review").fetchall()
    assert [dict(r) for r in stored] == [
        {"plan_id": plan_id, "balance_cents": 126000, "blocked": None, "model": config.load().llm_model}
    ]
    # the audit says a model was asked, once
    rows = conn.execute("SELECT actor, result FROM agent_action WHERE tool = 'web.review_plan' ORDER BY id").fetchall()
    assert [r["actor"] for r in rows] == ["web:owner@example.com"] and rows[0]["result"]["model"] == "ok"


def test_a_review_the_model_cannot_give_still_carries_the_money_and_the_check(conn, fake):
    from agent import plan_review

    fake.error = "the assistant is busy; try again in a minute"
    funded_prepaid(conn)
    add_plan_by_hand(conn, draft_sections(), biz="biz_1", status="draft")
    out = plan_review.review(conn, config.load(), "biz_1", by=plan_review.By.owner(**OWNER))
    assert out["model"] == "unavailable" and out["summary"] is None and out["checks"] == []
    assert out["money"]["runway_days"] == 38 and out["blocked"] is None
    assert conn.execute("SELECT count(*) AS n FROM plan_review").fetchone()["n"] == 0  # asked again next time


def test_no_draft_plan_means_nothing_to_review(conn, fake):
    from agent import plan_review

    funded_prepaid(conn)
    assert plan_review.review(conn, config.load(), "biz_1", by=plan_review.By.owner(**OWNER)) is None
    plan_id = add_plan_by_hand(conn, sections(), biz="biz_1", status="approved")
    assert plan_review.review(conn, config.load(), "biz_1", by=plan_review.By.owner(**OWNER)) is None and plan_id


def test_the_launch_checks_refusal_is_the_review_s_blocked_and_its_ask(conn, fake, live):  # noqa: F811
    """An own-account business whose knowledge has no phone: the launch's own check refuses with NO_PHONE
    (ads_run.precheck, run as a read), so the review carries it and the matching ask -- the phone, a text question
    the owner answers on the card, in their assistant (update_business_profile) or by e-mail. Google bills an own
    account, so there is no money to read."""
    from agent import ads_run, plan_review
    from agent.tests.test_ads_run import _knowledge, seed

    seed(conn, status="draft", secs=draft_sections())
    _knowledge(conn, phone=None)
    out = plan_review.review(conn, config.load(), "biz_1", by=plan_review.By.owner(**OWNER))
    assert out["blocked"] == ads_run.NO_PHONE and out["money"] is None
    assert out["ask"] == {"id": "phone", "input": "text", "text": ads_run.NO_PHONE, "tool": "update_business_profile"}
    assert fake.review_calls[0]["material"]["blocked"] == ads_run.NO_PHONE
    # the phone in place, the refusal is gone -- and that is a new look, not the stored one
    _knowledge(conn, phone="+1 650 555 0100")
    out = plan_review.review(conn, config.load(), "biz_1", by=plan_review.By.owner(**OWNER))
    assert out["blocked"] is None and out["ask"] is None and len(fake.review_calls) == 2


@pytest.mark.parametrize(
    ("sentence", "ask", "resolution"),
    [
        ("NO_PHONE", ("phone", "text", "update_business_profile"), ("ask", "phone")),
        ("NO_LOCATION", ("location", "address", "update_business_profile"), ("ask", "location")),
        ("NO_RADIUS", ("radius", "radius", "update_marketing_plan"), ("ask", "radius")),
        ("OVER_LIMIT", ("budget", None, "update_marketing_budget"), ("decide", "budget")),
        ("UNDER_FLOOR", ("budget", None, "update_marketing_budget"), ("decide", "budget")),
        ("NO_PAGE", ("page", None, "update_landing_page"), ("decide", "page")),
        ("PAGE_UNCHECKED", ("page", None, "update_landing_page"), ("fix", "check_page")),
        ("CHECKING_PAGE", ("page", None, "update_landing_page"), ("fix", "check_page")),
        ("PAGE_FIX", ("page", None, "update_landing_page"), ("fix", "page_fix")),
        ("NO_FUNDS", ("funds", None, "get_billing"), ("decide", "funds")),
        ("FIRST_PAYMENT", ("funds", None, "get_billing"), ("decide", "funds")),
        ("TOO_LITTLE_COPY", ("plan", None, "update_marketing_plan"), ("fix", "write_copy")),
        ("NOT_SEARCH_PLAN", ("plan", None, "update_marketing_plan"), ("fix", "fresh_plan")),
        ("NOT_LINKED", ("account", None, "connect_account"), ("decide", "account")),
        ("LINK_PENDING", ("account", None, "connect_account"), ("decide", "account")),
    ],
)
def test_every_refusal_the_launch_can_give_has_an_ask_and_a_resolution(sentence, ask, resolution):
    """Each sentence the launch's check refuses with (ads_run, the Google Search adapter) maps to what Admiry asks the
    owner for -- a question with its input where the owner types the answer, an action otherwise; always the tool an
    assistant calls with it -- and, B0 (spec 2026-10-08 §3.1), to what Admiry offers on its own surfaces: its kind (fix,
    ask, decide) and its resolution. Filled sentences (a label, a floor, a fix) match their template."""
    from agent import ads_run, plan_review
    from agent.channels.google_search import adapter as google

    template = getattr(ads_run, sentence, None) or getattr(google, sentence)
    text = template.format(label="Google Search", floor="$200", fix="put the phone in the footer")
    got = plan_review.ask_for(text)
    assert (got["id"], got.get("input"), got["tool"]) == ask and got["text"] == text
    found = plan_review.resolution_for(text)
    assert (found["kind"], found["resolution"]) == resolution
    assert plan_review.template_for(text) == template
    for nothing in ("Something the launch never says", "", None):
        assert plan_review.ask_for(nothing) is None and plan_review.resolution_for(nothing) is None


def test_the_resolutions_cover_every_refusal_and_a_question_is_an_ask():
    """B0: one row per refusal template, of three kinds; a refusal whose ask the owner answers in words (an `input`) is
    the `ask` kind, and no other is."""
    from agent import plan_review

    asks = dict(plan_review.ASKS)
    assert set(plan_review.RESOLUTIONS) == set(asks)
    assert {row["kind"] for row in plan_review.RESOLUTIONS.values()} == {"fix", "ask", "decide"}
    typed = {template for template, ask in asks.items() if ask.get("input")}
    assert {t for t, row in plan_review.RESOLUTIONS.items() if row["kind"] == "ask"} == typed
    assert set(plan_review.PLACEHOLDERS) == {asks[t]["id"] for t in typed}


def test_a_refusal_over_two_places_is_read_by_its_first_places_sentence():
    """B0 P12, controller's fix (Task 11): ads_run._nothing_launches names each place before a reason that does not
    name it. The MCP side's ask is unchanged from before B0 (`ask_for`): a refusal that leads with another place's
    name matches no row, since an assistant still gets no ask for it, as ever. The home reads the first place's
    sentence with its name taken off instead (`resolution_for`, `ask_id_for`): it still asks its one question or
    offers its one choice."""
    from agent import ads_run, plan_review

    joined = f"Google Search: {ads_run.NO_PHONE}. NewsBreak: Add funds to start NewsBreak."
    assert plan_review.ask_for(joined) is None
    assert plan_review.resolution_for(joined) == {"kind": "ask", "resolution": "phone"}
    assert plan_review.ask_id_for(joined) == "phone"


# ---------------------------------------------------------------- the card: review, then Confirm and launch
def _funded_on_the_card(client, conn, google) -> tuple[str, str]:
    """The prepaid path as the card walks it, then the webhook's credit (which verifies the business) and a draft
    plan. Returns (csrf, plan_id)."""
    from agent.tests.test_setup_api import setup_call
    from agent.tests.test_web import claimed_business, csrf_of

    claimed_business(client, conn, google)
    csrf = csrf_of(client)
    r = setup_call(client, "choose", business_id="biz_1", mode="managed", csrf=csrf).json()
    docs = {d["id"]: d["version"] for d in r["state"]["documents"]}
    assert setup_call(client, "accept", business_id="biz_1", documents=docs, csrf=csrf).json()["state"]["step"] == "pay"
    fund(conn, cents=126000)
    return csrf, add_plan_by_hand(conn, draft_sections(), biz="biz_1", status="draft")


def test_the_owners_yes_approves_and_launches(client, conn, google):
    from psycopg.types.json import Jsonb

    from agent import ads_run, business, db
    from agent.tests.test_ads_run import CHECK_OK, KNOWLEDGE, PAGE_URL
    from agent.tests.test_setup_api import setup_call

    csrf, plan_id = _funded_on_the_card(client, conn, google)
    # launch_check (L1) reads the checklist whatever the execution switch says (unlike the old precheck, which
    # skipped it here since nothing is on): a page and a geocoded address so the list has nothing blocking
    business.add_knowledge(conn, "biz_1", KNOWLEDGE, source="owner")
    conn.execute(
        "INSERT INTO page (id, business_id, source, url, status, check_json) VALUES (%s, 'biz_1', 'owner', %s, "
        "'published', %s)",
        (db.new_id("pg"), PAGE_URL, Jsonb(CHECK_OK)),
    )
    st = setup_call(client, "state", business_id="biz_1").json()["state"]
    assert st["step"] == "done" and st["plan_id"] == plan_id and st["plan_status"] == "draft"
    r = setup_call(client, "approve", business_id="biz_1", plan_id=plan_id, csrf=csrf).json()
    assert r["ok"] and r["state"]["step"] == "done", r
    assert r["launch"] == {"channels": {}, "say": ads_run.OFF}  # every switch is off here: the launch says so
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "approved"
    row = conn.execute(
        "SELECT actor, approval_kind, approval_ref FROM agent_action WHERE tool = 'web.approve_plan'"
    ).fetchone()
    assert row["actor"] == "web:owner@example.com" and row["approval_kind"] == "web"
    assert conn.execute("SELECT status FROM proposal WHERE id = %s", (row["approval_ref"],)).fetchone()["status"] == (
        "confirmed"
    )


def test_approve_is_a_change_and_needs_the_csrf_token(client, conn, google):
    from agent.tests.test_setup_api import setup_call

    _csrf, plan_id = _funded_on_the_card(client, conn, google)
    assert setup_call(client, "approve", business_id="biz_1", plan_id=plan_id).status_code == 403
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "draft"


def test_approve_takes_the_plan_the_card_reviewed_not_another(client, conn, google, fake):
    from agent import setup
    from agent.tests.test_setup_api import setup_call

    csrf, plan_id = _funded_on_the_card(client, conn, google)
    for stale in ("pl_other", None, 7):
        r = setup_call(client, "approve", business_id="biz_1", plan_id=stale, csrf=csrf)
        assert r.status_code == 409 and r.json() == {"ok": False, "error": setup.PLAN_CHANGED}
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "draft"


def test_nothing_to_review_or_approve_is_refused_in_the_owner_s_words(conn):
    from agent import setup

    funded_prepaid(conn)  # money in, no plan
    owner = setup.Owner(**OWNER)
    with pytest.raises(setup.Refused) as e:
        setup.approve(config.load(), owner, "biz_1", "pl_x")
    assert str(e.value) == setup.NOTHING_TO_REVIEW and e.value.status == 409


def test_approve_answers_with_the_launch_checks_refusal_and_approves_nothing(conn, live):  # noqa: F811
    from agent import ads_run, setup
    from agent.tests.test_ads_run import _knowledge, seed

    _key, plan_id = seed(conn, status="draft", secs=draft_sections())  # own account, live link: verified
    agreements.accept(conn, business_id="biz_1", email="owner@example.com", ip=None)  # the card's accept step
    _knowledge(conn, phone=None)
    st = setup.state(conn, config.load(), "biz_1")
    assert st["step"] == "done" and st["plan_status"] == "draft"
    with pytest.raises(setup.Refused) as e:
        setup.approve(config.load(), setup.Owner(**OWNER), "biz_1", plan_id)
    assert str(e.value) == ads_run.NO_PHONE and e.value.status == 409
    assert conn.execute("SELECT status FROM plan WHERE id = %s", (plan_id,)).fetchone()["status"] == "draft"
    assert conn.execute("SELECT count(*) AS n FROM channel_campaign").fetchone()["n"] == 0


# ---------------------------------------------------------------- a plan with no ad text (2026-10-08)
# The founder's dev walk, 2026-10-08: a plan drawn before the plan's draft always carried its ad text (c50e156a), and
# adjusted since, has none; the launch refuses it (TOO_LITTLE_COPY), and the second look offers Admiry writing it
# (`blockers.write_copy`, B0: from the conversation, not the card). The home's words after a plan read only as an
# adjustment, the plan page approves only.
NO_COPY = {"headlines": [], "descriptions": []}
REVIEW_OF_NO_COPY = models.PlanReview(
    summary="Google Search ads for Invisalign within 5 miles, about $33 a day.",
    checks=[
        models.ReviewCheck(topic="services", ok=False, text="The searches match Invisalign, but there is no ad text."),
        models.ReviewCheck(topic="compliance", ok=True, text="No ad text was provided, so there are no banned claims."),
    ],
    grounded=True,
    grades=REVIEW.grades,
    missing=[],
)


def test_a_plan_without_ad_text_never_passes_its_claims(conn, fake):
    """No ad text the launch would take: the look offers Admiry writing it (`write_copy`, the conversation's button,
    B0), and the compliance check is Admiry's own sentence, never the model's pass -- nothing has been checked yet."""
    from agent import plan_review

    fake.reviews = [REVIEW_OF_NO_COPY]
    funded_prepaid(conn)
    add_plan_by_hand(conn, draft_sections(**NO_COPY), biz="biz_1", status="draft")
    out = plan_review.review(conn, config.load(), "biz_1", by=plan_review.By.owner(**OWNER))
    assert out["checks"] == [
        REVIEW_OF_NO_COPY.checks[0].model_dump(),
        {"topic": "compliance", "ok": False, "text": plan_review.NO_COPY_CHECKED},
    ]
    stored = conn.execute("SELECT review FROM plan_review").fetchone()["review"]
    assert stored["checks"][1]["ok"] is True  # the model's answer is kept as it came; the owner reads Admiry's


def test_a_plan_with_its_ad_text_keeps_the_models_checks(conn, fake):
    from agent import plan_review

    fake.reviews = [REVIEW_OF_NO_COPY]
    funded_prepaid(conn)
    add_plan_by_hand(conn, draft_sections(), biz="biz_1", status="draft")
    out = plan_review.review(conn, config.load(), "biz_1", by=plan_review.By.owner(**OWNER))
    assert out["checks"] == [c.model_dump() for c in REVIEW_OF_NO_COPY.checks]


def _without_copy(conn, biz: str) -> dict:
    """The latest plan's Google Search entry stripped of its words, as a plan from before c50e156a stored it."""
    from psycopg.types.json import Jsonb

    plan = plans.latest(conn, biz)
    secs = plan["sections"]
    for ch in secs["ads"]["channels"]:
        ch.update(NO_COPY)
    conn.execute("UPDATE plan SET json = jsonb_set(json, '{sections}', %s) WHERE id = %s", (Jsonb(secs), plan["id"]))
    return plans.get(conn, biz, plan["id"])


# ---------------------------------------------------------------- the home: the owner's yes once funded
def test_the_home_offers_the_yes_once_the_balance_is_funded(client, conn, google, home_fake):  # noqa: F811 - the fixture is used by name
    """The launch card of a prepaying business with money in and a draft plan offers the owner's yes (drawn by its
    id, "confirm"), not prepaying again -- on a reload and on a change to the plan alike; MCP's `next` (launch_next
    without `funded`) is unchanged."""
    from agent import tools_consult
    from agent.tests.test_home_api import WAYS, _claimed_plan, _reloaded, turn

    _claimed_plan(client, conn, google, prepaid=True)
    biz = turn(client, {"action": "state"}).json()["result"]["business_id"]  # the walk's own business
    nxt, _note = _reloaded(client)
    assert [a["id"] for a in nxt["actions"][len(WAYS) :]] == ["prepay", "adjust"]  # not funded yet: prepay
    agreements.accept(conn, business_id=biz, email="owner@example.com", ip=None)
    agreements.accept(conn, business_id=biz, email="owner@example.com", ip=None, document=legal.ADDENDUM)
    fund(conn, biz=biz, cents=126000)
    nxt, note = _reloaded(client)
    # drawn by its id on the home (L1 review M1): never the dict, which a first launch under review would differ by
    assert [a["id"] for a in nxt["actions"][len(WAYS) :]] == ["confirm", "adjust"] and note is None
    assert tools_consult.CONFIRM_ACTION == {  # its tool is the public launch
        "id": "confirm",
        "label": "Confirm and launch",
        "tool": "launch_marketing_plan",
        "primary": True,
    }
    changed = turn(client, {"action": "adjust", "monthly_budget_usd": 1500, "idempotency_key": "a2"}).json()
    assert changed["ok"], changed
    assert [a["id"] for a in changed["result"]["next"]["actions"][len(WAYS) :]] == ["confirm", "adjust"]


def test_the_home_offers_no_launch_while_the_first_one_is_in_review(client, conn, google, home_fake, monkeypatch):  # noqa: F811 - the fixture is used by name
    """R15 d: while the latest plan is in Admiry's review, the home's reload offers no way to launch (`next` and
    `launch_note` none) and says so (`in_review`), never the ways to run the ads or the owner's yes again."""
    from psycopg.types.json import Jsonb

    from agent import business, db, launch_review, setup
    from agent.tests.test_ads_run import CHECK_OK, KNOWLEDGE, PAGE_URL
    from agent.tests.test_home_api import _claimed_plan, _reloaded, turn

    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "on")
    built = _claimed_plan(client, conn, google, prepaid=True)
    biz = turn(client, {"action": "state"}).json()["result"]["business_id"]  # the walk's own business
    agreements.accept(conn, business_id=biz, email="owner@example.com", ip=None)
    agreements.accept(conn, business_id=biz, email="owner@example.com", ip=None, document=legal.ADDENDUM)
    fund(conn, biz=biz, cents=126000)
    # launch_check (L1) reads the checklist whatever the execution switch says: a phone and a page so it has
    # nothing blocking (as test_the_owners_yes_approves_and_launches)
    business.add_knowledge(conn, biz, KNOWLEDGE, source="owner")
    conn.execute(
        "INSERT INTO page (id, business_id, source, url, status, check_json) VALUES (%s, %s, 'owner', %s, "
        "'published', %s)",
        (db.new_id("pg"), biz, PAGE_URL, Jsonb(CHECK_OK)),
    )
    plan_id = built["data"]["plan_id"]
    out = setup.approve(config.load(), setup.Owner(email="owner@example.com", ip=None), biz, plan_id)
    assert out["approved"]["plan_status"] == "in_review"
    nxt, note = _reloaded(client)
    assert nxt is None and note is None
    state = turn(client, {"action": "state"}).json()["result"]
    assert state["in_review"] == launch_review.IN_REVIEW_SAY


def test_the_funded_yes_is_labelled_by_whether_it_is_the_first_launch(client, conn, google, home_fake, monkeypatch):  # noqa: F811 - the fixture is used by name
    """L1 review (fix round 2, item 3): with AGENT_LAUNCH_REVIEW on, the funded yes for a business's first launch
    reads "Confirm this plan" (D6: the review gates a first launch only) on a reload (_state_result) and on a
    change (_funded_next) alike; a funded newer version under an already-approved plan stays "Confirm and launch"."""
    from agent.tests.test_ads_run import add_plan
    from agent.tests.test_home_api import _claimed_plan, _reloaded, turn

    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "on")
    _claimed_plan(client, conn, google, prepaid=True)
    biz = turn(client, {"action": "state"}).json()["result"]["business_id"]  # the walk's own business
    agreements.accept(conn, business_id=biz, email="owner@example.com", ip=None)
    agreements.accept(conn, business_id=biz, email="owner@example.com", ip=None, document=legal.ADDENDUM)
    fund(conn, biz=biz, cents=126000)
    nxt, _note = _reloaded(client)
    assert next(a for a in nxt["actions"] if a["id"] == "confirm")["label"] == "Confirm this plan"
    changed = turn(client, {"action": "adjust", "monthly_budget_usd": 1500, "idempotency_key": "a2"}).json()
    assert changed["ok"], changed
    actions = changed["result"]["next"]["actions"]
    assert next(a for a in actions if a["id"] == "confirm")["label"] == "Confirm this plan"
    # the latest draft approved and running: a newer draft on top of it funds the same way, but now the yes
    # approves and launches at once, whatever AGENT_LAUNCH_REVIEW says (D6: the review gates a first launch only)
    conn.execute("UPDATE plan SET status = 'approved' WHERE business_id = %s", (biz,))
    add_plan(conn, draft_sections(monthly_usd=1800.0), biz=biz, status="draft")
    nxt2, _note2 = _reloaded(client)
    assert next(a for a in nxt2["actions"] if a["id"] == "confirm")["label"] == "Confirm and launch"


@needs_node
def test_the_home_s_launch_card_draws_the_yes_alone_and_leading(client):
    """home.js's launch card given the funded answer (CONFIRM_ACTION, a change): one way, so "Next step:" above it, the
    confirm button in the primary style, the change as a chip -- the home.html button the card keeps by the action's
    id, and its label is the server's, "Confirm and launch" (test_home_page's harness over the page's own script)."""
    from agent import envelope, tools_consult
    from agent.tests.test_home_page import LAUNCH_HARNESS, _launch_words, _node

    words = _launch_words(client)
    cases = [{"next": envelope.actions(tools_consult.CONFIRM_ACTION, tools_consult.ADJUST_ACTION)}]
    assert _node(LAUNCH_HARNESS, {"words": words, "cases": cases}) == [
        {"launch": "Next step:", "signIn": None, "buttons": ["confirm"], "dark": ["confirm"], "chips": ["adjust"]}
    ]


# ---------------------------------------------------------------- the assistant: asked for what the launch needs
def _view(blocked: str | None) -> dict:
    steps = [{"id": s, "label": s, "done": True, "tool": None} for s in ("sign_in", "plan", "account", "page")]
    return {"ready_to_launch": blocked is None, "blocked": blocked, "steps": steps}


def test_the_assistant_is_asked_for_what_the_launch_needs_instead_of_nothing():
    """MarketingState's `next` for a plan the launch refuses used to be NONE with the refusal in `say` alone (the
    Google launch trace, 2026-10-07): now the refusal's ask -- a question the owner answers in words, carrying the
    tool the answer goes to, or the action that fixes it; NONE only for a refusal no ask knows."""
    from agent import ads_run, envelope, tools_state

    say, nxt = tools_state._launch_left("Bright Smile Dental", "$1,000", _view(ads_run.NO_PHONE), draft=False)
    assert (
        say == f"The plan for Bright Smile Dental is ready: $1,000 a month. It cannot launch yet: {ads_run.NO_PHONE}."
    )
    assert nxt["kind"] == "question" and nxt["question"] == {
        "id": "phone",
        "index": 1,
        "count": 1,
        "prompt": ads_run.NO_PHONE,
        "input": "text",
        "options": [],
        "other": {"allowed": True, "placeholder": "The business phone, with its area code"},
        "tool": "update_business_profile",
    }
    _say, nxt = tools_state._launch_left("x", "$1,000", _view(ads_run.OVER_LIMIT), draft=False)
    assert nxt == envelope.actions(
        {"id": "budget", "label": "Change the budget", "tool": "update_marketing_budget", "primary": True}
    )
    _say, nxt = tools_state._launch_left("x", "$1,000", _view("Something the launch never says"), draft=False)
    assert nxt == envelope.NONE


# ---------------------------------------------------------------- the e-mail: the next step after a payment
def test_the_payment_mail_says_what_comes_next(client, conn, google, stripe, resend, fake, monkeypatch):
    """The owner who paid and closed the tab learns the next step from the receipt: where its review stands once a
    Checkout naming the plan sends a first launch to review (L1 Task 8), or -- while the launch's checklist refuses
    the plan -- the one thing Admiry needs first; the confirmation on the home otherwise."""
    from psycopg.types.json import Jsonb

    from agent import business, db, launch_review, plan_review, setup
    from agent.tests.test_ads_run import CHECK_OK, KNOWLEDGE, PAGE_URL
    from agent.tests.test_stripe_hook import deliver, event, started

    started(client, conn, google)
    # launch_check (L1) reads the checklist whatever the execution switch says: a page and a geocoded address so the
    # list has nothing blocking (as test_the_owners_yes_approves_and_launches)
    business.add_knowledge(conn, "biz_1", KNOWLEDGE, source="owner")
    conn.execute(
        "INSERT INTO page (id, business_id, source, url, status, check_json) VALUES (%s, 'biz_1', 'owner', %s, "
        "'published', %s)",
        (db.new_id("pg"), PAGE_URL, Jsonb(CHECK_OK)),
    )
    plan_id = add_plan_by_hand(conn, draft_sections(), biz="biz_1", status="draft")
    assert deliver(client, event()).json()["credited"] is True
    (mail,) = [m for m in resend.sent if m["to"] == ["owner@example.com"]]
    assert plan_review.REVIEW_NEXT.format(link="http://testserver/?setup=biz_1") in mail["text"]
    # the words, by what stands in the way
    assert plan_review.next_step_words(conn, "biz_1", "http://testserver") == plan_review.REVIEW_NEXT.format(
        link="http://testserver/?setup=biz_1"
    )

    # L1 Task 8: a Checkout naming this draft, with the review on, sends a first launch to review once it is paid --
    # the receipt says where it stands, not that it is ready to confirm
    monkeypatch.setenv("AGENT_LAUNCH_REVIEW", "on")
    owner = setup.Owner(email="owner@example.com", ip=None)
    setup.pay(config.load(), owner, "biz_1", "300", plan_id=plan_id)
    assert deliver(client, event(session_id="cs_test_2", amount=31020, event_id="evt_2")).json()["credited"] is True
    assert plans.get(conn, "biz_1", plan_id)["status"] == plans.IN_REVIEW
    mail2 = [m for m in resend.sent if m["to"] == ["owner@example.com"]][-1]
    assert launch_review.IN_REVIEW_SAY in mail2["text"]
    assert plan_review.next_step_words(conn, "biz_1", "http://testserver") == launch_review.IN_REVIEW_SAY

    conn.execute("UPDATE plan SET status = 'approved' WHERE id = %s", (plan_id,))
    assert plan_review.next_step_words(conn, "biz_1", "http://testserver") is None  # nothing waits


# ---------------------------------------------------------------- the card as drawn (static/setup.js under node)
# A stub DOM that keeps listeners and a `fetch` that answers each action as the server would: `state` with the state
# given, `review` with the review given, `approve` with the approved state and the launch's words, `fact` with the
# review unblocked. Prints the card's texts, the calls made, the Confirm button's state, then the same after the
# click or the phone's Save (argv[3]: the phone typed; none types nothing and clicks Confirm when it is enabled;
# "refresh" asks the card to look again; "busy" asks it while the first look is on its way: that look is held, then
# answered with `stale` -- a review and its blocker of the plan before the fix -- and every later look with the review).
REVIEW_HARNESS = r"""
const src = require("fs").readFileSync(process.argv[1], "utf8");
const [state, review, approved, blocker, stale] = JSON.parse(process.argv[2]);
const typed = process.argv[3] || "";
const calls = [];
let held = null; // "busy": lets the first look's answer go
const node = (tag) => ({ tag, className: "", text: "", kids: [], style: {}, dataset: {}, listeners: {}, disabled: false,
  classList: { add() {}, remove() {}, toggle() {} },
  set textContent(v) { this.text = String(v); this.kids = []; }, get textContent() { return this.text; },
  append(...cs) { this.kids.push(...cs); }, prepend(...cs) { this.kids.unshift(...cs); },
  replaceChildren(...cs) { this.kids = cs; }, setAttribute() {}, remove() {},
  addEventListener(ev, fn) { this.listeners[ev] = fn; }, reportValidity() { return true; } });
Object.assign(global, { window: {}, document: { createElement: node, body: node("body") }, location: { assign() {} },
  fetch: async (url, opts) => {
    const body = JSON.parse(opts.body);
    calls.push(body);
    if (body.action === "review" && stale && !held) {
      return new Promise((ok) => (held = () => ok({ json: async () => ({ ok: true, state, ...stale }) })));
    }
    if (body.action === "review") return { json: async () => ({ ok: true, state, review, blocker }) };
    if (body.action === "approve") return { json: async () => ({ ok: true, ...approved }) };
    return { json: async () => ({ ok: true, state }) };
  } });
eval(src);
const host = node("host");
const extra = JSON.parse(process.argv[4] || "{}");
const api = window.AdmirySetup.run({ host, businessId: "biz_1", ...extra, pick: null, paid: false, csrf: async () => "t" });
const texts = (n) => [n.text, ...n.kids.flatMap(texts)].filter(Boolean);
const find = (n, pred) => (pred(n) ? n : n.kids.map((k) => find(k, pred)).find(Boolean));
const said = (c) => (c.action === "approve" ? `${c.action}:${c.plan_id}` : c.action);
setTimeout(() => {
  const card = host.kids[0];
  const confirm = find(card, (k) => k.tag === "button" && k.text === "Confirm and launch");
  const out = { texts: texts(card), calls: calls.map(said), confirm: confirm ? { disabled: confirm.disabled } : null,
    businesses: calls.map((c) => c.business_id || null) };
  const phone = find(card, (k) => k.tag === "input" && k.name === "phone");
  out.phone = Boolean(phone);
  if (typed === "refresh") api.refresh();
  else if (typed === "busy") api.refresh(), held();
  else if (confirm && !confirm.disabled) confirm.listeners.click();
  setTimeout(() => {
    const after = host.kids[0], button = find(after, (k) => k.tag === "button" && k.text === "Confirm and launch");
    out.after = { texts: texts(after), calls: calls.map(said), confirm: button ? { disabled: button.disabled } : null };
    console.log(JSON.stringify(out));
  }, 40);
}, 80);
"""
REVIEW_STATE = {
    "name": "Bright Smile Dental",
    "step": "review",
    "plan_id": "pl_1",
    "plan_url": "http://testserver/plan/pl_1",
    "mode": "managed",
    "locked": None,
    "documents": [],
    "link": None,
    "link_mode": "manual",
    "balance_usd": 1260,
    "pending_topups": 0,
    "open_topups": 0,
    "transfer_pending_usd": None,
    "paused": False,
    "topup": None,
    "payments_ready": True,
    "steps": [],
    "ways": ["managed"],
    "ways_note": None,
}
REVIEW_ANSWER = {
    "plan_id": "pl_1",
    "blocked": None,
    "ask": None,
    "money": {"balance_usd": 1260, "daily_usd": 33, "runway_days": 38},
    "summary": REVIEW.summary,
    "checks": [c.model_dump() for c in REVIEW.checks],
    "grounded": True,
    "model": "ok",
}
APPROVED = {
    "state": {**REVIEW_STATE, "step": "done"},
    "launch": {
        "channels": {"google_search": {"state": "live", "say": "Google Search is live."}},
        "say": "Google Search is live.",
    },
}


def _drawn(
    state: dict,
    review: dict,
    approved: dict,
    typed: str = "",
    blocker: dict | None = None,
    stale: dict | None = None,
    options: dict | None = None,
) -> dict:
    import json
    import subprocess
    from pathlib import Path

    js = Path(__file__).parents[1] / "static" / "setup.js"
    done = subprocess.run(
        [
            "node",
            "-e",
            REVIEW_HARNESS,
            str(js),
            json.dumps([state, review, approved, blocker, stale]),
            typed,
            json.dumps(options or {}),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
def test_a_card_opened_under_a_plan_sets_up_the_business_its_page_shows():
    """Follow-up R1: a card opened from the conversation names the business its page shows (`page`), so a tab left
    open while another tab switched the business still sets up the one it shows -- never the cookie's new one."""
    out = _drawn(REVIEW_STATE, REVIEW_ANSWER, APPROVED, options={"businessId": None, "page": "biz_page"})
    assert out["calls"] and set(out["businesses"]) == {"biz_page"}


# setup.js back from Stripe (`paid`): the card follows the balance and says when the money is in (`onSettled`, follow-up
# R4) -- once, after the look that shows it landed; never while it still waits. The card's 3-second waits run at once.
# A "fail" entry among the states answers that look with a refusal (as the network or the server can). `refresh`: the
# conversation asks the card to look again (a fix made there) that many milliseconds in. Prints what each look saw, the
# card's words as each look found them (`shots`: the first before any look), and its words once the follow is over
# (`end`).
PAID_HARNESS = r"""
const src = require("fs").readFileSync(process.argv[1], "utf8");
const states = JSON.parse(process.argv[2]);
const options = JSON.parse(process.argv[3]);
const wait = setTimeout;
global.setTimeout = (fn, ms) => wait(fn, ms >= 1000 ? 10 : ms);
const slow = Boolean(options.slow);
const seen = [], shots = [];
const texts = (n) => [n.text, ...n.kids.flatMap(texts)].filter(Boolean);
const node = (tag) => ({ tag, className: "", text: "", kids: [], style: {}, dataset: {}, listeners: {}, disabled: false,
  classList: { add() {}, remove() {}, toggle() {} },
  set textContent(v) { this.text = String(v); this.kids = []; }, get textContent() { return this.text; },
  append(...cs) { this.kids.push(...cs); }, prepend(...cs) { this.kids.unshift(...cs); },
  replaceChildren(...cs) { this.kids = cs; }, setAttribute() {}, remove() {},
  addEventListener(ev, fn) { this.listeners[ev] = fn; }, reportValidity() { return true; } });
Object.assign(global, { window: {}, document: { createElement: node, body: node("body") }, location: { assign() {} },
  fetch: async (url, opts) => {
    const body = JSON.parse(opts.body);
    const state = body.action === "state" && states.length > 1 ? states.shift() : states[0];
    if (body.action === "state") {
      shots.push(texts(host));
      seen.push(state === "fail" ? "failed" : state.balance_usd ? "landed" : "waiting");
    }
    if (state === "fail") return { json: async () => ({ ok: false, error: "x" }) };
    if (body.action === "review" && slow) await new Promise((ok) => wait(ok, 30));
    return { json: async () => ({ ok: true, state }) };
  } });
eval(src);
const host = node("host");
const card = window.AdmirySetup.run({ host, businessId: "biz_1", pick: null, paid: true, csrf: async () => "t",
  onSettled: () => seen.push("settled") });
if (options.refresh) wait(() => card.refresh(), options.refresh);
wait(() => console.log(JSON.stringify({ seen, shots, end: texts(host) })), 800);
"""
# paid at Stripe, not credited yet: the server's step is `pay` (balance 0) with the top-up open, as setup.state gives it
WAITING = {
    **REVIEW_STATE,
    "step": "pay",
    "balance_usd": 0.0,
    "pending_topups": 1,
    "open_topups": 1,
    "topup": {
        "suggested_usd": 470,
        "min_usd": 200,
        "max_usd": 5000,
        "daily_usd": 33,
        "rails": ["card"],
        "rates": {"card_bps": 330, "card_fixed_cents": 30, "ach_bps": 80, "ach_cap_cents": 500},
    },
}
LANDED = {**REVIEW_STATE, "step": "done"}  # credited: open_topups 0, a balance
W_LOOK = {**REVIEW_STATE, "step": "done", "balance_usd": 0, "open_topups": 1}  # a later top-up not credited yet
BEING_CONFIRMED = "Your payment is being confirmed; this updates when it lands."
NOT_YET = "Your payment has not shown up yet. Reload this page in a few minutes before you pay again."


def _paid(states: list, slow: bool = False, card: bool = False, refresh: int | None = None) -> list | dict:
    """What each look saw ("failed" for a refused one) and "settled"; with `card`, also the card's words at each look
    and at the end; `refresh`, in milliseconds: when the conversation asks the card to look again."""
    import json
    import subprocess
    from pathlib import Path

    js = Path(__file__).parents[1] / "static" / "setup.js"
    options = json.dumps({"slow": slow, "refresh": refresh})
    args = ["node", "-e", PAID_HARNESS, str(js), json.dumps(states), options]
    done = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    return out if card else out["seen"]


def _pay_form(texts: list) -> list:
    """The pay form's buttons ("Pay $… by card") among the card's words."""
    return [t for t in texts if t.startswith("Pay $")]


@needs_node
def test_back_from_stripe_the_card_says_when_the_money_is_in():
    assert _paid([WAITING, WAITING, LANDED]) == ["waiting", "waiting", "landed", "settled"]


@needs_node
def test_money_already_in_is_said_at_once():
    assert _paid([LANDED]) == ["landed", "settled"]


@needs_node
def test_money_that_never_lands_is_never_said_in():
    seen = _paid([WAITING])
    assert "settled" not in seen and seen.count("waiting") == 21  # the first look and its twenty follows


@needs_node
def test_a_follow_that_meets_the_cards_own_look_waits_a_beat_and_still_says_the_money_is_in():
    """Task 1 review (a): a later top-up while the plan is a draft finds the card at its review step, its second look
    on its way when a follow comes; that follow waits a beat instead of ending the wait, so the money landing is
    still said (`onSettled`)."""
    assert _paid([W_LOOK, REVIEW_STATE], slow=True) == ["waiting", "landed", "settled"]


@needs_node
def test_while_a_payment_is_being_confirmed_the_card_asks_for_no_other():
    """Follow-up final review I2: back from Stripe with a first payment not credited yet, the server's step is `pay`,
    and the card drew its pay form -- "Pay $… by card" -- beside "being confirmed"; when its twenty looks ended, the
    note still said "this updates when it lands". Now the card draws no pay form while it waits, and when the follow
    gives up it says so, still without one until a reload."""
    out = _paid([WAITING], card=True)
    waiting = out["shots"][1:]  # the card as each follow found it: after the first look, still waiting
    assert len(waiting) == 20 and all(BEING_CONFIRMED in shot for shot in waiting)
    assert not [t for shot in waiting for t in _pay_form(shot)]
    assert NOT_YET in out["end"] and BEING_CONFIRMED not in out["end"]
    assert not _pay_form(out["end"])


@needs_node
def test_a_follow_that_fails_keeps_the_card_waiting_and_still_says_the_money_is_in():
    """Follow-up final review M6 (added by ruling): one look that failed -- the network, a refusal -- ended the wait, so
    the money landing was never said (`onSettled`), and with the wait over, the card's next drawing brought the pay
    form back beside a payment still landing (I2's guard reads the wait). A failed look keeps the card waiting: the
    next one sees the money in; while it waits, a look the conversation asks for draws no pay form; and twenty failed
    looks end still waiting, saying the payment has not shown up yet."""
    assert _paid([WAITING, "fail", LANDED]) == ["waiting", "failed", "landed", "settled"]
    out = _paid([WAITING, "fail", WAITING], card=True, refresh=100)  # a fix in the conversation meanwhile
    assert not _pay_form(out["end"]) and not [t for shot in out["shots"][2:] for t in _pay_form(shot)]
    out = _paid([WAITING, "fail"], card=True)  # every follow fails
    assert out["seen"] == ["waiting"] + ["failed"] * 20 and NOT_YET in out["end"] and not _pay_form(out["end"])
