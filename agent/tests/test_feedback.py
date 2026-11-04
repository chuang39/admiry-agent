"""Plan feedback (spec 2026-09-30 §6, D136, D137, D145): one vote per person per plan version with its snapshot, a
change updates it, 👎 with contact-me opens a ticket, the homepage shows the vote again, /plan shows the thumbs for a
sampled plan or a vote already given, and the refusals."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agent import feedback
from agent.tests.test_home_api import IP, fake, turn  # noqa: F401 - fake is a fixture used by name
from agent.tests.test_home_support import at_the_minute_limit
from agent.tests.test_oauth_consent import _sign_in
from agent.tests.test_transcript import planned


def vote(client, plan_id, csrf, **kw):
    return client.post("/api/feedback", json={"plan_id": plan_id, "surface": "home", "csrf": csrf, **kw})


def test_sampled_is_stable_and_near_the_rate():
    ids = [f"pl_{n:016x}" for n in range(10_000)]
    share = sum(feedback.sampled(i, 0.2) for i in ids) / len(ids)
    assert 0.18 < share < 0.22 and all(feedback.sampled(i, 0.2) == feedback.sampled(i, 0.2) for i in ids[:50])
    assert not any(feedback.sampled(i, 0) for i in ids[:100]) and all(feedback.sampled(i, 1) for i in ids[:100])


def test_a_vote_its_change_and_its_snapshot(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    _, csrf = planned(client, conn, google)
    plan_id = conn.execute("SELECT id FROM plan").fetchone()["id"]
    up = vote(client, plan_id, csrf, rating="up").json()
    assert up["ok"] and up["ticket_id"] is None
    down = vote(client, plan_id, csrf, rating="down", reasons=["budget", "area"], comment="Too pricey for us").json()
    assert down["feedback_id"] == up["feedback_id"]
    row = conn.execute("SELECT * FROM plan_feedback").fetchone()
    assert (row["rating"], row["reasons"], row["comment"], row["surface"]) == (
        "down",
        ["budget", "area"],
        "Too pricey for us",
        "home",
    )
    assert row["actor"] == "web:owner@example.com" and row["snapshot"]["plan"]["id"] == plan_id
    assert row["snapshot"]["turns"][-1]["text"].startswith("Here is the plan")
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 0
    s = turn(client, {"action": "state"}).json()
    assert s["result"]["feedback"] == {"rating": "down", "reasons": ["budget", "area"], "comment": "Too pricey for us"}
    assert conn.execute("SELECT count(*) AS n FROM agent_action WHERE tool = 'web.feedback'").fetchone()["n"] == 2


def test_a_down_vote_with_contact_me_opens_a_ticket(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    _, csrf = planned(client, conn, google)
    plan_id = conn.execute("SELECT id FROM plan").fetchone()["id"]
    r = vote(client, plan_id, csrf, rating="down", reasons=["copy"], contact_me=True).json()
    t = conn.execute("SELECT * FROM support_ticket").fetchone()
    assert r["ticket_id"] == t["id"] and (t["source"], t["reason"]) == ("feedback", "dissatisfied")
    words = conn.execute("SELECT body FROM ticket_event WHERE kind = 'owner_words'").fetchone()["body"]
    assert words == feedback.REASONS["copy"]
    assert conn.execute("SELECT ticket_id FROM plan_feedback").fetchone()["ticket_id"] == t["id"]
    assert [m["to"] for m in support_on.sent] == [["owner@example.com"], ["ops@example.com"]]


def test_the_refusals(client, conn, fake, support_on, google, monkeypatch):  # noqa: F811 - fixture used by name
    _, csrf = planned(client, conn, google)
    plan_id = conn.execute("SELECT id FROM plan").fetchone()["id"]
    assert client.post("/api/feedback", json={"plan_id": plan_id, "surface": "home", "rating": "up"}).status_code == 403
    for bad in (
        {"rating": "meh"},
        {"surface": "email"},
        {"reasons": ["price"]},
        {"reasons": "budget"},
        {"comment": "x" * 1001},
        {"contact_me": "yes"},
        {"plan_id": "pl_nope"},
    ):
        body = {"plan_id": plan_id, "surface": "home", "rating": "down", "csrf": csrf, **bad}
        assert client.post("/api/feedback", json=body).status_code == 400, bad
    assert vote(client, "pl_" + "0" * 16, csrf, rating="up").status_code == 404
    other = _sign_in(client, google, email="someone@example.com")
    assert vote(client, plan_id, other, rating="up").status_code == 403
    client.cookies.clear()
    assert vote(client, plan_id, other, rating="up").status_code == 401
    monkeypatch.setenv("AGENT_SUPPORT", "off")
    assert vote(client, plan_id, other, rating="up").status_code == 404


def test_the_plan_page_shows_the_thumbs_for_a_sampled_plan_or_a_given_vote(
    client,
    conn,
    fake,  # noqa: F811 - fixture used by name
    support_on,
    google,
    monkeypatch,
):
    _, csrf = planned(client, conn, google)
    plan_id = conn.execute("SELECT id FROM plan").fetchone()["id"]
    monkeypatch.setenv("AGENT_FEEDBACK_PLAN_PAGE_RATE", "0")
    assert "data-rate-plan" not in client.get(f"/plan/{plan_id}").text
    monkeypatch.setenv("AGENT_FEEDBACK_PLAN_PAGE_RATE", "1")
    page = client.get(f"/plan/{plan_id}").text
    assert f'data-rate-plan="{plan_id}"' in page and "/static/support.js" in page and "/static/support.css" in page
    monkeypatch.setenv("AGENT_FEEDBACK_PLAN_PAGE_RATE", "0")
    vote(client, plan_id, csrf, rating="up")
    page = client.get(f"/plan/{plan_id}").text
    assert "data-rate-plan" in page and '"rating": "up"' in page  # the vote shows, sampled or not
    client.cookies.clear()
    assert "data-rate-plan" not in client.get(f"/plan/{plan_id}").text  # only the signed-in owner sees them


def test_the_script_s_reasons_are_feedback_s():
    js = (Path(__file__).parents[1] / "static" / "support.js").read_text()
    for rid, label in feedback.REASONS.items():
        assert f'["{rid}", "{label}"]' in js, rid


def test_a_hostile_comment_stays_inside_the_data_vote_attribute(client, conn, fake, support_on, google, monkeypatch):  # noqa: F811 - fixture used by name
    """The comment comes back on /plan in a single-quoted attribute: `tojson` escapes ' < > & so it can neither close
    the attribute nor open a tag, and the script still reads the words back whole."""
    _, csrf = planned(client, conn, google)
    plan_id = conn.execute("SELECT id FROM plan").fetchone()["id"]
    monkeypatch.setenv("AGENT_FEEDBACK_PLAN_PAGE_RATE", "0")
    hostile = "x'><img src=x onerror=alert(1)>&\"</script>"
    assert vote(client, plan_id, csrf, rating="down", comment=hostile).json()["ok"]
    page = client.get(f"/plan/{plan_id}").text
    assert hostile not in page and "<img src=x" not in page  # the words never appear as markup
    attribute = re.search(r"class=\"rate-host\"[^>]*data-vote='([^']*)'", page)
    assert attribute and json.loads(attribute.group(1))["comment"] == hostile  # and the script still reads them whole


def test_the_plan_page_s_thumbs_sit_in_main_under_the_last_card(client, conn, fake, support_on, google, monkeypatch):  # noqa: F811 - fixture used by name
    """Inside the main landmark, under the Approve card: page.css's column sizes the box, so it needs no rule of its
    own."""
    planned(client, conn, google)
    plan_id = conn.execute("SELECT id FROM plan").fetchone()["id"]
    monkeypatch.setenv("AGENT_FEEDBACK_PLAN_PAGE_RATE", "1")
    page = client.get(f"/plan/{plan_id}").text
    main = page[page.index("<main") : page.index("</main>")]
    assert "data-rate-plan" in main and main.index('id="approve"') < main.index("data-rate-plan")
    assert "/static/support.css" in main and "/static/support.js" in main


def test_a_nul_byte_in_the_comment_never_reaches_the_database(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    """Postgres refuses a NUL in a TEXT column (support.py's _clean drops one from ticket words): the comment's column
    is a second place it would land, so the endpoint drops it from the comment before either write."""
    _, csrf = planned(client, conn, google)
    plan_id = conn.execute("SELECT id FROM plan").fetchone()["id"]
    r = vote(client, plan_id, csrf, rating="down", comment="too\x00 pricey", contact_me=True)
    assert r.status_code == 200 and r.json()["ok"]
    assert conn.execute("SELECT comment FROM plan_feedback").fetchone()["comment"] == "too pricey"
    assert conn.execute("SELECT body FROM ticket_event WHERE kind = 'owner_words'").fetchone()["body"] == "too pricey"
    only = vote(client, plan_id, csrf, rating="down", comment="\x00 \x00")  # nothing else in it: no comment at all
    assert only.status_code == 200 and conn.execute("SELECT comment FROM plan_feedback").fetchone()["comment"] is None


def test_a_changed_vote_refreshes_the_snapshot(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    """Spec §6: a second vote by the same person on the same version updates the row and refreshes its snapshot."""
    _, csrf = planned(client, conn, google)
    plan_id = conn.execute("SELECT id FROM plan").fetchone()["id"]
    vote(client, plan_id, csrf, rating="up")
    first = conn.execute("SELECT created_at, updated_at, snapshot FROM plan_feedback").fetchone()
    vote(client, plan_id, csrf, rating="down", reasons=["copy"])
    rows = conn.execute("SELECT created_at, updated_at, snapshot FROM plan_feedback").fetchall()
    assert len(rows) == 1 and rows[0]["created_at"] == first["created_at"]  # the same row
    assert rows[0]["updated_at"] > first["updated_at"]
    assert rows[0]["snapshot"]["built_at"] != first["snapshot"]["built_at"]  # built again for the second vote


def test_a_down_vote_with_contact_me_joins_the_ticket_that_is_already_open(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    """Spec §4.5 (one open ticket per business) and §6: the vote's words are appended to it, not a second ticket."""
    _, csrf = planned(client, conn, google)
    plan_id = conn.execute("SELECT id FROM plan").fetchone()["id"]
    ask = "Can someone call me about the budget?"
    assert client.post("/api/support", json={"text": ask, "csrf": csrf}, headers=IP).json()["ok"]
    ticket = conn.execute("SELECT id FROM support_ticket").fetchone()["id"]
    r = vote(client, plan_id, csrf, rating="down", reasons=["budget"], contact_me=True).json()
    assert r["ticket_id"] == ticket and conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 1
    words = conn.execute("SELECT via, body FROM ticket_event WHERE kind = 'owner_words' ORDER BY created_at").fetchall()
    assert [(w["via"], w["body"]) for w in words] == [("home", ask), ("feedback", feedback.REASONS["budget"])]
    assert conn.execute("SELECT ticket_id FROM plan_feedback").fetchone()["ticket_id"] == ticket


def test_a_new_version_starts_with_no_vote_and_the_earlier_vote_stays(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    """Spec §6: an adjustment makes a new version and the row shows its empty state; the earlier vote stays."""
    _, csrf = planned(client, conn, google)
    first = conn.execute("SELECT id FROM plan").fetchone()["id"]
    vote(client, first, csrf, rating="down", reasons=["budget"], comment="Too much")
    assert turn(client, {"action": "state"}).json()["result"]["feedback"]["rating"] == "down"
    turn(client, {"action": "adjust", "level": "conservative", "idempotency_key": "j1"})
    second = conn.execute("SELECT id FROM plan ORDER BY version DESC").fetchone()["id"]
    assert second != first
    state = turn(client, {"action": "state"}).json()["result"]
    assert state["plan_id"] == second and state["feedback"] is None
    assert conn.execute("SELECT plan_id, rating FROM plan_feedback").fetchall() == [
        {"plan_id": first, "rating": "down"}
    ]
    vote(client, second, csrf, rating="up")  # the new version takes a vote of its own; the first one is untouched
    assert turn(client, {"action": "state"}).json()["result"]["feedback"] == {
        "rating": "up",
        "reasons": [],
        "comment": None,
    }
    assert conn.execute("SELECT rating FROM plan_feedback WHERE plan_id = %s", (first,)).fetchone()["rating"] == "down"


def test_a_vote_counts_toward_the_visitor_key_s_limits(client, conn, fake, support_on, google):  # noqa: F811 - fixture used by name
    """A vote from the homepage is one of its calls: a visitor key at its per-minute limit gets the homepage's 429 and
    nothing is written -- no vote, no ticket, no audit row."""
    _, csrf = planned(client, conn, google)
    plan_id = conn.execute("SELECT id FROM plan").fetchone()["id"]
    at_the_minute_limit(conn)
    rows = conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"]
    r = vote(client, plan_id, csrf, rating="down", reasons=["budget"], contact_me=True)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert conn.execute("SELECT count(*) AS n FROM plan_feedback").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM support_ticket").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM agent_action").fetchone()["n"] == rows


@pytest.mark.parametrize("csrf", [123, ["t"], "é"])
def test_a_csrf_that_is_not_a_token_is_refused_not_a_crash(client, conn, fake, support_on, google, csrf):  # noqa: F811 - fixture used by name
    """A JSON body can carry any value as its csrf: anything but the session's token is a 403, never a 500."""
    planned(client, conn, google)
    plan_id = conn.execute("SELECT id FROM plan").fetchone()["id"]
    assert vote(client, plan_id, csrf, rating="up").status_code == 403
    assert conn.execute("SELECT count(*) AS n FROM plan_feedback").fetchone()["n"] == 0


HOME_JS = Path(__file__).parents[1] / "static" / "home.js"

# home.js is one closure over the page, so this reads plan() and redraw() out of its text and runs them under Node with
# a tree that has just what they touch (append moves a node, remove, replaceWith, a class lookup) and stand-ins for the
# card builders and for AdmirySupport.rate, which records every mount. It prints what it saw after each step: a load
# with a vote, the same version drawn again (redraw), the same version introduced again, and a new version.
HARNESS = r"""
const vm = require("vm");
const lines = require("fs").readFileSync(process.argv[1], "utf8").split("\n");
const { a: A, b: B, vote, typed } = JSON.parse(process.argv[2]);
const grab = (name) => { // home.js keeps its functions two spaces in and ends each with a line of "  }"
  const from = lines.findIndex((l) => l.startsWith(`  function ${name}(`));
  const to = lines.findIndex((l, i) => i > from && l === "  }");
  if (from < 0 || to < 0) throw new Error(`home.js has no function ${name} in the shape this reads`);
  return lines.slice(from, to + 1).join("\n");
};
class Node {
  constructor(cls) { this.className = cls; this.kids = []; this.up = null; }
  append(n) { n.remove(); n.up = this; this.kids.push(n); return n; }
  remove() { if (this.up) this.up.kids.splice(this.up.kids.indexOf(this), 1); this.up = null; }
  replaceWith(n) { n.remove(); n.up = this.up; this.up.kids.splice(this.up.kids.indexOf(this), 1, n); this.up = null; }
  get isConnected() { let n = this; while (n.up) n = n.up; return n === log; }
  querySelectorAll(sel) { // one class per selector: ".a, .b"
    const want = sel.split(",").map((s) => s.trim().slice(1)), out = [];
    const walk = (n) => n.kids.forEach((k) => {
      if (want.some((w) => k.className.split(" ").includes(w))) out.push(k);
      walk(k);
    });
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
const log = new Node("log"), S = { planNode: null, planId: null }, mounts = [];
const ctx = vm.createContext({
  S, log, window: { AdmirySupport: { rate: (host, opts) => (mounts.push(opts), host.append(new Node("rate"))) } },
  clone: () => new Node("plan"), add: (n) => log.append(n), admiry: () => new Node("turn"),
  planCard() {}, budgetCard() {}, economicsCard() {}, whyCard() {}, adPreviewCard() {}, pageCard() {}, launchCard() {},
  changesLine() {}, progress() {}, reveal() {},
});
vm.runInContext(grab("plan") + "\n" + grab("redraw"), ctx);
const rows = () => log.querySelectorAll(".rate").length, plans = () => log.querySelectorAll(".plan").length;
const rowOf = (n) => n.querySelector(".rate");
const seen = {};

ctx.plan({ cards: {}, plan_id: A, feedback: vote }, null, "Here is the plan."); // a load: the state carries the vote
const first = rowOf(S.planNode), firstPlan = S.planNode;
first.typed = typed; // what the owner has done to the row since: a pressed button, a form open, words in it
seen.load = { mounts: mounts.length, planId: mounts[0]?.planId, surface: mounts[0]?.surface, vote: mounts[0]?.vote,
  rows: rows() };

ctx.redraw(); // the same version again: a chosen level tapped, a slider let go where it was, a failed adjust
seen.redraw = { mounts: mounts.length, sameRow: rowOf(S.planNode) === first, typed: first.typed,
  oldPlanHasNone: rowOf(firstPlan) === null, replaced: S.planNode !== firstPlan, plans: plans(), rows: rows() };

ctx.plan({ cards: {}, plan_id: A }, null, "Done."); // the same version, introduced again
seen.again = { mounts: mounts.length, sameRow: rowOf(S.planNode) === first, plans: plans(), rows: rows() };

ctx.plan({ cards: {}, plan_id: B }, null); // an adjustment made version B: its row starts empty
const second = rowOf(S.planNode);
seen.adjust = { mounts: mounts.length, planId: mounts[1]?.planId, vote: mounts[1]?.vote,
  freshRow: second !== null && second !== first && second.typed === undefined,
  firstGone: !first.isConnected, rows: rows() };
console.log(JSON.stringify(seen));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
def test_a_redraw_keeps_the_versions_thumbs_and_a_new_version_starts_empty():
    """Spec §6: an adjustment makes a new version and its row is empty; the same version drawn again (a level tapped
    that is already chosen, a slider let go where it was, a failed adjust) keeps the row it has, with the vote and any
    half-filled 👎 form."""
    a, b, vote, typed = "pl_" + "a" * 16, "pl_" + "b" * 16, {"rating": "down", "reasons": ["budget"]}, "half a comment"
    args = json.dumps({"a": a, "b": b, "vote": vote, "typed": typed})
    run = subprocess.run(["node", "-e", HARNESS, str(HOME_JS), args], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    seen = json.loads(run.stdout)
    assert seen["load"] == {"mounts": 1, "planId": a, "surface": "home", "vote": vote, "rows": 1}
    assert seen["redraw"] == {
        "mounts": 1,
        "sameRow": True,
        "typed": typed,
        "oldPlanHasNone": True,
        "replaced": True,
        "plans": 1,
        "rows": 1,
    }
    assert seen["again"] == {"mounts": 1, "sameRow": True, "plans": 1, "rows": 1}
    assert seen["adjust"] == {"mounts": 2, "planId": b, "vote": None, "freshRow": True, "firstGone": True, "rows": 1}
