"""K5 (R-K5, P7, P8): the founder's grading page on the dev box -- every frozen plan as the judge reads it, the six
dimensions graded 1-5 with a note, saved as they are given; a 404 for everyone else and everywhere on prod."""

import pytest

from agent import eval_web
from agent.eval import cases
from agent.roles import contracts
from agent.tests.eval_fixtures import ADMIN, golden_set
from agent.tests.test_ads_run import DESCRIPTIONS, HEADLINES
from agent.tests.test_web import sign_in

DIMS = contracts.PLAN.ids
JSON = {"Accept": "application/json"}
# fields of the knowledge the golden cases leave empty, and the page the ads open: filled in so the page shows them
WEBSITE = "https://brightsmile.example/dental"
REVIEWS = "Patients praise short waits & gentle hygienists"
LANDING = "Invisalign for teens"


@pytest.fixture
def golden(tmp_path, monkeypatch):
    """A golden set of two frozen plans (dentists-1 and dentists-2) the page reads in place of the image's."""
    cases_dir, flawed_dir = golden_set(tmp_path)
    for path in sorted(cases_dir.glob("*.json")):
        data = cases.raw(path)
        data["knowledge"] |= {"website": WEBSITE, "reviews_summary": REVIEWS}
        data["plan"]["sections"]["ads"]["channels"][0]["landing_service"] = LANDING
        cases.write(path, data)
    monkeypatch.setattr(eval_web, "CASES_DIR", cases_dir)
    monkeypatch.setattr(eval_web, "FLAWED_DIR", flawed_dir)
    return [cases.digest(it.plan) for it in eval_web._items()]


@pytest.fixture
def founder(client, google, golden, monkeypatch):
    """The listed founder, signed in with Google."""
    monkeypatch.setenv("AGENT_EVAL_GRADERS", ADMIN)
    google.email = ADMIN
    sign_in(client, "/ops/eval")
    return client


def _csrf(html: str) -> str:
    return html.split('name="csrf" value="')[1].split('"')[0]


def _nothing_saved(conn) -> bool:
    graded = conn.execute("SELECT count(*) AS n FROM eval_grade").fetchone()["n"]
    audited = conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'web.eval_grade'").fetchone()["n"]
    return graded == 0 and audited == 0


def test_the_grading_page_is_404_for_everyone_but_the_listed_on_dev(client, conn, google, golden, monkeypatch):
    key = golden[0]
    assert client.get("/ops/eval").status_code == 404  # no AGENT_EVAL_GRADERS: no page
    assert client.get(f"/ops/eval/plan/{key}").status_code == 404
    monkeypatch.setenv("AGENT_EVAL_GRADERS", ADMIN)
    page = client.get("/ops/eval")
    assert page.status_code == 200 and "/connect/google/start?next=/ops/eval" in page.text  # anonymous: sign in
    assert "Grade the golden set" not in page.text
    assert (
        client.post(f"/ops/eval/plan/{key}", data={"grade_fit": "4"}).status_code == 404
    )  # anonymous: no page to post
    sign_in(client, "/connect")  # owner@example.com: signed in, not listed
    assert client.get("/ops/eval").status_code == 404
    assert client.get(f"/ops/eval/plan/{key}").status_code == 404
    assert client.post(f"/ops/eval/plan/{key}", data={"grade_fit": "4"}).status_code == 404
    monkeypatch.delenv("AGENT_EVAL_GRADERS")
    monkeypatch.setenv("AGENT_ENV", "prod")
    google.email = ADMIN
    sign_in(client, "/connect")
    assert client.get("/ops/eval").status_code == 404 and client.get(f"/ops/eval/plan/{key}").status_code == 404
    assert client.post(f"/ops/eval/plan/{key}", data={"grade_fit": "4"}).status_code == 404
    assert _nothing_saved(conn)


def test_the_listed_email_matches_whatever_its_case(client, google, golden, monkeypatch):
    monkeypatch.setenv("AGENT_EVAL_GRADERS", " Founder@Example.COM ")  # read lower-cased
    google.email = "FOUNDER@example.com"  # a session keeps the e-mail lower-cased, and the gate compares it so
    sign_in(client, "/ops/eval")
    assert client.get("/ops/eval").status_code == 200


def test_the_founder_sees_every_plan_as_the_judge_reads_it_and_nothing_more(founder, golden):
    index = founder.get("/ops/eval")
    assert index.status_code == 200 and "0 of 2 plans graded on all 6 dimensions" in index.text
    assert index.headers["content-security-policy"].startswith("default-src 'self'")
    plan = founder.get(f"/ops/eval/plan/{golden[0]}")
    assert plan.headers["content-security-policy"].startswith("default-src 'self'")
    html = plan.text
    footnote = eval_web._items()[0].plan["sections"]["estimate"]["footnote"]
    for words in (
        *HEADLINES,
        *DESCRIPTIONS,
        "invisalign palo alto",
        "jobs",
        footnote,
        "Services and area",
        "Clear to the owner",
    ):
        assert words in html, words
    assert "dentists-1" not in html and "dentists-2" not in html and "eval_dentists" not in html  # no case, no flaw
    assert "(serious)" not in html  # the judge's checks are the judge's
    assert 'name="grade_fit" value="5"' in html and '<script src="/static/eval.js"></script>' in html
    assert "<script>" not in html


def test_the_page_shows_the_whole_of_what_the_judge_reads_of_the_business_and_of_each_ad(founder, golden):
    """P8: the founder grades on the judge's material -- the phone, the website, what a customer is worth (the revenue
    estimate is computed from it), the reviews, the services the owner wants promoted, and the page each ad opens --
    not on less (escaped as everything on the page is)."""
    for item in eval_web._items():
        html = founder.get(f"/ops/eval/plan/{cases.digest(item.plan)}").text
        known = item.knowledge
        assert known["phone"] in html and WEBSITE in html and "waits &amp; gentle hygienists" in html
        assert f"${known['typical_customer_value_usd']:,.0f}" in html
        assert REVIEWS not in html  # the "&" arrives escaped, never raw
        focus = known["goal_hint"]["focus_services"]
        assert (f"<td>{', '.join(focus)}</td>" in html) == bool(focus)  # the second case names none
        assert f"<h3>Page it opens</h3><p>{LANDING}</p>" in html
    assert "eval_dentists" not in html  # the listing's place id names the case: still not shown


def test_grades_save_as_they_are_given_and_the_list_counts_them(founder, conn, golden):
    first, second = golden
    csrf = _csrf(founder.get(f"/ops/eval/plan/{first}").text)
    r = founder.post(f"/ops/eval/plan/{first}", data={"csrf": csrf, "grade_fit": "4", "note_fit": "ok"}, headers=JSON)
    assert r.status_code == 200 and r.json() == {"graded": 1, "dimensions": 6}
    r = founder.post(f"/ops/eval/plan/{first}", data={"csrf": csrf, "note_copy": "promises a cure"}, headers=JSON)
    assert r.json()["graded"] == 1
    full = {"csrf": csrf, **{f"grade_{d}": "3" for d in DIMS}, **{f"note_{d}": "" for d in DIMS}}
    r = founder.post(f"/ops/eval/plan/{first}", data=full, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/ops/eval/plan/{second}"
    rows = conn.execute("SELECT grade, grader FROM eval_grade").fetchall()
    assert len(rows) == 6 and all(row["grade"] == 3 and row["grader"] == ADMIN for row in rows)
    assert "1 of 2 plans graded on all 6 dimensions" in founder.get("/ops/eval").text
    assert 'name="grade_fit" value="3" checked' in founder.get(f"/ops/eval/plan/{first}").text
    audit = conn.execute(
        "SELECT business_id, actor, args_redacted, result FROM agent_action WHERE tool = 'web.eval_grade' ORDER BY id"
    ).fetchall()
    assert len(audit) == 3 and all(a["business_id"] is None and a["actor"] == f"web:{ADMIN}" for a in audit)
    assert all(a["args_redacted"] == {"plan": first} for a in audit)
    assert [a["result"] for a in audit] == [{"graded": 1}, {"graded": 1}, {"graded": 6}]
    last = founder.post(f"/ops/eval/plan/{second}", data={"csrf": csrf, "grade_fit": "5"}, follow_redirects=False)
    assert last.headers["location"] == "/ops/eval"


def test_a_grade_needs_the_forms_csrf_and_a_whole_number_from_one_to_five(founder, conn, golden):
    key = golden[0]
    csrf = _csrf(founder.get(f"/ops/eval/plan/{key}").text)
    assert founder.post(f"/ops/eval/plan/{key}", data={"grade_fit": "4"}).status_code == 403
    assert founder.post(f"/ops/eval/plan/{key}", data={"csrf": "wrong", "grade_fit": "4"}).status_code == 403
    assert founder.post(f"/ops/eval/plan/{key}", data={"csrf": csrf, "grade_fit": "7"}).status_code == 400
    assert founder.post(f"/ops/eval/plan/{key}", data={"csrf": csrf, "grade_fit": "4.5"}).status_code == 400
    assert founder.post(f"/ops/eval/plan/{key}", data={"csrf": csrf, "grade_fit": "0"}).status_code == 400
    assert founder.post("/ops/eval/plan/0000000000000000", data={"csrf": csrf, "grade_fit": "4"}).status_code == 404
    assert _nothing_saved(conn)


def test_a_form_with_one_bad_field_saves_nothing_at_all(founder, conn, golden):
    """The whole form is checked before anything is written: no partial save, no audit row."""
    key = golden[0]
    csrf = _csrf(founder.get(f"/ops/eval/plan/{key}").text)
    good = {"csrf": csrf, "grade_fit": "4", "note_fit": "fine", "grade_budget": "5", "note_budget": "fine"}
    for bad in ({"grade_copy": "9"}, {"grade_copy": "x"}, {"note_copy": "w" * (cases.NOTE_MAX + 1)}):
        r = founder.post(f"/ops/eval/plan/{key}", data=good | bad, headers=JSON)
        assert r.status_code == 400, bad
    assert _nothing_saved(conn)


def test_blank_grades_notes_at_the_limit_and_unknown_fields_are_accepted(founder, conn, golden):
    key = golden[0]
    csrf = _csrf(founder.get(f"/ops/eval/plan/{key}").text)
    data = {
        "csrf": csrf,
        "grade_fit": " 5 ",  # padding is dropped, as the grade of a hand-typed post
        "grade_budget": "",  # no choice made yet: not an error, nothing graded
        "note_budget": "x" * cases.NOTE_MAX,
        "note_copy": "a\r\nb",  # a browser sends a textarea's line breaks as CRLF
        "grade_nonsense": "99",  # not a dimension: ignored
        "other": "field",
    }
    r = founder.post(f"/ops/eval/plan/{key}", data=data, headers=JSON)
    assert r.status_code == 200 and r.json() == {"graded": 1, "dimensions": 6}
    rows = {r["dimension"]: r for r in conn.execute("SELECT dimension, grade, note FROM eval_grade").fetchall()}
    assert set(rows) == {"fit", "budget", "copy"} and rows["fit"]["grade"] == 5 and rows["budget"]["grade"] is None
    assert rows["budget"]["note"] == "x" * cases.NOTE_MAX
    assert rows["copy"]["note"] == "a\nb"
    # a full textarea (maxlength counts each line break once) reaches the server with each break as two characters
    full_box = ("z" * 499 + "\r\n") * 2
    assert len(full_box) == cases.NOTE_MAX + 2
    r = founder.post(f"/ops/eval/plan/{key}", data={"csrf": csrf, "note_clarity": full_box}, headers=JSON)
    assert r.status_code == 200
    note = conn.execute("SELECT note FROM eval_grade WHERE dimension = 'clarity'").fetchone()["note"]
    assert note == "z" * 499 + "\n" + "z" * 499  # the breaks counted once, the note's trailing break trimmed
