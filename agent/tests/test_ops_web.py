"""L1 Task 11 (spec 2026-10-10 §7): /ops/review -- the queue and one review with its decisions, for a listed admin
signed in with Google, on every box; a 404 for everyone else; the sign-in page for a visitor while the list is set."""

import pytest

from agent import ads_run, launch_review, plans
from agent.tests.test_ads_run import BIZ, seed
from agent.tests.test_ops_review import _refused_row
from agent.tests.test_review_launch import draft_sections
from agent.tests.test_web import sign_in

ADMIN = "founder@example.com"


@pytest.fixture
def review_id(conn) -> str:
    seed(conn, status="draft", secs=draft_sections())
    return launch_review.submit(conn, BIZ, plans.latest(conn, BIZ)["id"], approval={"kind": "paid"})["review_id"]


@pytest.fixture
def admin(client, google, monkeypatch):
    monkeypatch.setenv("AGENT_ADMIN_EMAILS", ADMIN)
    google.email = ADMIN
    sign_in(client, "/ops/review")
    return client


def _csrf(html: str) -> str:
    return html.split('name="csrf" value="')[1].split('"')[0]


def test_no_list_no_page_and_an_owner_is_never_let_in(client, conn, google, review_id, monkeypatch):
    assert client.get("/ops/review").status_code == 404
    assert client.get(f"/ops/review/{review_id}").status_code == 404
    monkeypatch.setenv("AGENT_ADMIN_EMAILS", ADMIN)
    page = client.get("/ops/review")
    assert page.status_code == 200 and "/connect/google/start?next=/ops/review" in page.text
    assert client.post(f"/ops/review/{review_id}", data={"action": "approve"}).status_code == 404
    sign_in(client, "/connect")  # owner@example.com: signed in, not listed
    assert client.get("/ops/review").status_code == 404
    assert client.post(f"/ops/review/{review_id}", data={"action": "approve"}).status_code == 404


def test_the_listed_admin_reads_the_queue_and_the_review(admin, review_id):
    q = admin.get("/ops/review")
    assert q.status_code == 200 and review_id in q.text and "Bright Smile Dental" in q.text
    d = admin.get(f"/ops/review/{review_id}")
    assert d.status_code == 200 and "Approve and launch" in d.text and "Return to the owner" in d.text


def test_approve_and_launch_from_the_page(admin, conn, review_id, monkeypatch):
    monkeypatch.setattr(ads_run, "launch_now", lambda biz, **k: {"channels": {}, "say": ""})
    csrf = _csrf(admin.get(f"/ops/review/{review_id}").text)
    r = admin.post(f"/ops/review/{review_id}", data={"action": "approve", "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/ops/review/{review_id}?done=approve"
    assert launch_review.get(conn, review_id)["state"] == "passed"


def test_a_return_without_a_note_is_said_on_the_page_and_changes_nothing(admin, conn, review_id):
    csrf = _csrf(admin.get(f"/ops/review/{review_id}").text)
    r = admin.post(f"/ops/review/{review_id}", data={"action": "return", "note": "", "csrf": csrf})
    assert r.status_code == 409 and "Say in a sentence what the owner should change." in r.text
    assert launch_review.get(conn, review_id)["state"] == "queued"


def test_a_decision_without_the_token_is_refused(admin, review_id):
    assert admin.post(f"/ops/review/{review_id}", data={"action": "approve"}).status_code == 403


def test_prod_serves_the_page_to_a_listed_admin(admin, review_id, monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "prod")
    assert admin.get("/ops/review").status_code == 200


def test_the_page_shows_googles_refusals(admin, conn, review_id):
    page = admin.get(f"/ops/review/{review_id}").text
    assert "Google&#39;s refusals" in page or "Google's refusals" in page
    _refused_row(conn, review_id, launch_review.get(conn, review_id)["plan_id"])
    page = admin.get(f"/ops/review/{review_id}").text
    assert "Invisalign Deals" in page and "TRADEMARKS_IN_AD_TEXT (PROHIBITED)" in page and "dropped" in page
