"""L1 Task 5 (spec 2026-10-10 §4.3): the plan card checks a draft before it says Ready to launch -- Checking, then one row
per problem with what Admiry suggests and its field or one-tap fix, then Ready to launch with the promise. A fix draws
the plan again (the card checks again); the yes posts the plan the card shows. home.js's own functions run under Node
over a small DOM of their own, as test_home_blocker did."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

AGENT = Path(__file__).parents[1]
HOME_JS = AGENT / "static" / "home.js"
HOME_HTML = AGENT / "templates" / "home.html"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="drives home.js with node")
PROMISE = (
    "Your ads go live within 1 hour to 1 business day. Admiry e-mails you if anything needs your attention; write to "
    "hello@admiry.ai any time."
)


def _default_heading() -> str:
    """The plan card's heading as home.html ships it by default -- never "Ready to launch" (founder's rule: that
    word appears only once the list has been checked and is empty)."""
    m = re.search(r'<p[^>]*data-part="launch-k"[^>]*>([^<]*)</p>', HOME_HTML.read_text())
    assert m, "home.html has no launch-k heading in the shape this reads"
    return m.group(1)


DEFAULT_HEADING = _default_heading()

HARNESS = r"""
const vm = require("vm");
const lines = require("fs").readFileSync(process.argv[1], "utf8").split("\n");
const input = JSON.parse(process.argv[2]);
const grab = (name) => {
  const from = lines.findIndex((l) => l.startsWith(`  function ${name}(`) || l.startsWith(`  async function ${name}(`));
  const to = lines.findIndex((l, i) => i > from && l === "  }");
  if (from < 0 || to < 0) throw new Error(`home.js has no function ${name} in the shape this reads`);
  return lines.slice(from, to + 1).join("\n");
};
const line = (start) => {
  const found = lines.find((l) => l.startsWith(start));
  if (!found) throw new Error(`home.js has no line starting "${start}"`);
  return found;
};
class Node {
  constructor(tag = "div", cls = "", text = "") {
    Object.assign(this, { tag, cls: new Set(cls.split(" ").filter(Boolean)), text, kids: [], up: null, dataset: {},
      listeners: {}, attrs: {}, disabled: false, hidden: false, value: "", isConnected: true });
  }
  get classList() {
    const c = this.cls;
    return { add: (x) => c.add(x), remove: (x) => c.delete(x), contains: (x) => c.has(x),
      toggle: (x, on) => ((on === undefined ? !c.has(x) : on) ? c.add(x) : c.delete(x)) };
  }
  get className() { return [...this.cls].join(" "); }
  get children() { return this.kids; }
  set textContent(v) { this.text = String(v); }
  get textContent() { return this.text; }
  append(...ks) { ks.forEach((k) => { if (k.up) k.remove(); k.up = this; this.kids.push(k); }); }
  replaceChildren(...ks) { this.kids.forEach((k) => (k.up = null)); this.kids = []; this.append(...ks); }
  remove() { if (this.up) this.up.kids.splice(this.up.kids.indexOf(this), 1); this.up = null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(ev, fn) { this.listeners[ev] = fn; }
  focus() {}
  all() { return this.kids.flatMap((k) => [k, ...k.all()]); }
  matches(sel) { return sel.split(",").some((s) => (s = s.trim()).startsWith(".") ? this.cls.has(s.slice(1)) : this.tag === s); }
  querySelectorAll(sel) { return this.all().filter((n) => n.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
const log = new Node("div", "log"), calls = [];
const el = (tag, cls, text) => new Node(tag, cls || "", text == null ? "" : String(text));
const admiry = (text) => {
  const n = new Node("section", "turn"), body = new Node("div", "says");
  body.dataset.part = "body";
  n.append(new Node("p", "who", "Admiry"), body);
  body.append(new Node("p", "msg", text));
  log.append(n);
  return n;
};
const card = new Node("section", "card launch");
for (const p of ["launch-k", "checking", "checklist", "promise", "status"]) {
  const c = new Node(p === "checklist" ? "ol" : "p"); c.dataset.part = p; c.hidden = p !== "launch-k"; card.append(c);
}
card.all().find((n) => n.dataset.part === "launch-k").textContent = input.defaultHeading; // home.html's real default
log.append(card);
const S = { planId: "pl_1", planNode: card, businessId: "biz_1", setup: null };
const ctx = vm.createContext({
  log, S, el, calls, uuid: () => "u", part: (root, name) => root.all().find((n) => n.dataset.part === name) || null,
  admiry: (t) => { calls.push(`admiry:${t}`); return admiry(t); }, you: (t) => { calls.push(`you:${t}`); return new Node("section", "turn you"); },
  announce: (t) => calls.push(`announce:${t}`), reveal() {}, work: async (fn) => fn(), csrf: async () => "t",
  visitor() {}, handle: (r) => calls.push(`handle:${r.error}`), oops: (t) => calls.push(`oops:${t}`),
  plan: (d) => calls.push(`plan:${d.plan_id}`), openSetup: (...a) => calls.push(`setup:${a[0]}:${a[5] ? "agree" : ""}`),
  buildPage: () => calls.push("buildPage"), loadPageState() {}, location: { reload: () => calls.push("reload") },
  fetch: async (path) => ({ json: async () => (calls.push(`get:${path}`), input.check) }),
  post: async (path, body) => { calls.push(`post:${path}:${JSON.stringify(body)}`); return (input.posts || {})[path] || { ok: true }; },
  turn: async (action) => (calls.push(`turn:${action}`), (input.turns || {})[action] || { ok: true }),
});
const code = [line("  const INPUTS = "), line("  const OWN_WORDS = ")];
for (const f of ["get", "checkPlan", "drawCheck", "checkRow", "fixButton", "answerForm", "fix", "budgetChoice",
  "resolve", "resolved", "spentFix", "unspend", "recheck", "confirmPlan"]) code.push(grab(f));
code.push("({ checkPlan, drawCheck, confirmPlan });");
const api = vm.runInContext(code.join("\n"), ctx);
(async () => {
  let pending = null;
  for (const step of input.steps) {
    if (step.heading !== undefined) card.all().find((n) => n.dataset.part === "launch-k").textContent = step.heading;
    if (step.checkStart) {
      pending = api.checkPlan(card); // not awaited: read the heading mid-flight, before the GET resolves
      calls.push(`mid-heading:${card.all().find((n) => n.dataset.part === "launch-k").text}`);
    }
    if (step.check) await api.checkPlan(card);
    if (step.draw) api.drawCheck(card, step.draw);
    if (step.press) await card.all().filter((b) => b.tag === "button" && b.text === step.press)[0].listeners.click();
    if (step.answer) {
      const form = card.all().find((n) => n.cls.has("fix-answer"));
      form.all().find((n) => n.tag === "input").value = step.answer;
      await form.listeners.submit({ preventDefault() {} });
    }
    if (step.confirm) await api.confirmPlan({ id: "confirm", label: "Confirm and launch" });
  }
  if (pending) await pending;
  const rows = card.all().filter((n) => n.tag === "li").map((li) => ({
    id: li.dataset.id, text: li.all().map((n) => n.text).filter(Boolean),
    spent: li.cls.has("spent"), wait: li.cls.has("can-wait"),
    buttons: li.all().filter((n) => n.tag === "button").map((b) => b.text),
  }));
  const p = (name) => card.all().find((n) => n.dataset.part === name);
  console.log(JSON.stringify({ rows, calls, k: p("launch-k").text, ready: card.cls.has("launch-ready"),
    promise: !p("promise").hidden, checking: !p("checking").hidden, list: !p("checklist").hidden }));
})();
"""


def _run(**input) -> dict:
    input.setdefault("defaultHeading", DEFAULT_HEADING)  # home.html's real default (never "Ready to launch")
    out = subprocess.run(
        ["node", "-e", HARNESS, str(HOME_JS), json.dumps(input)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


PHONE = {
    "id": "phone",
    "plan_id": "pl_1",
    "text": "What number should the calls ring? Tell me the business phone first",
    "suggestion": "Calls from your ads ring this number.",
    "kind": "ask",
    "blocking": True,
    "note": None,
    "question": {"input": "tel", "placeholder": "The business phone, with its area code"},
    "actions": [],
}
PAGE = {
    "id": "page:compliance",
    "plan_id": "pl_1",
    "kind": "fix",
    "blocking": True,
    "note": None,
    "question": None,
    "text": "Your page needs a fix before your ads can send people there: Remove the banned words: best.",
    "suggestion": "Once it is changed, check again.",
    "actions": [{"id": "check_page", "label": "Check again", "primary": True}],
}
TERMS = {
    "id": "page:terms@newsbreak",
    "plan_id": "pl_1",
    "kind": "fix",
    "blocking": False,
    "question": None,
    "note": "NewsBreak starts once this is fixed; the rest can start without it.",
    "text": "Your page needs a fix before your ads can send people there: Add a Terms link in the footer.",
    "suggestion": "Once it is changed, check again.",
    "actions": [{"id": "check_page", "label": "Check again", "primary": True}],
}


@needs_node
def test_the_card_checks_then_lists_every_problem():
    out = _run(
        check={"ok": True, "check": {"plan_id": "pl_1", "items": [PHONE, PAGE, TERMS], "ready": False}},
        steps=[{"check": True}],
    )
    assert "get:/api/launch_check" in out["calls"] and not out["checking"] and out["list"]
    assert out["k"] == "2 things before it can launch" and not out["ready"] and not out["promise"]
    assert [r["id"] for r in out["rows"]] == ["phone", "page:compliance", "page:terms@newsbreak"]
    assert out["rows"][2]["wait"] and TERMS["note"] in out["rows"][2]["text"]
    assert out["rows"][1]["buttons"] == ["Check again"] and out["rows"][0]["buttons"] == ["Save"]


@needs_node
def test_a_clear_list_is_ready_to_launch_with_the_promise():
    out = _run(steps=[{"draw": {"plan_id": "pl_1", "items": [], "ready": True}}])
    assert out["k"] == "Ready to launch" and out["ready"] and out["promise"] and not out["list"]


@needs_node
def test_checking_says_so_before_the_heading_ever_claims_ready_to_launch():
    """Founder's rule: "Ready to launch" shows only once the list is checked and empty -- never while Admiry is
    still reading it (final review fix: the heading used to keep the template's default through the whole GET)."""
    out = _run(
        check={"ok": True, "check": {"plan_id": "pl_1", "items": [], "ready": True}},
        steps=[{"checkStart": True}],
    )
    assert "mid-heading:Checking your plan" in out["calls"]


@needs_node
def test_a_failed_check_leaves_a_neutral_heading_not_ready_to_launch():
    out = _run(
        check={"ok": False, "error": "Something went wrong on our side. Try again soon."},
        steps=[{"heading": "Ready to launch"}, {"check": True}],  # simulates a stale heading from an earlier draw
    )
    assert out["k"] == "Your plan"
    assert "handle:Something went wrong on our side. Try again soon." in out["calls"]


@needs_node
def test_a_list_for_another_plan_draws_nothing():
    out = _run(
        steps=[{"heading": "Ready to launch"}, {"draw": {"plan_id": "pl_old", "items": [PHONE], "ready": False}}]
    )
    assert out["rows"] == [] and not out["ready"]
    assert out["k"] == "Your plan"  # never left stuck on a stale "Ready to launch" (founder's rule)


@needs_node
def test_a_fix_posts_its_item_and_draws_the_plan_again():
    state = {"plan_id": "pl_2", "cards": {"plan": {}}, "next": {"kind": "none"}}
    out = _run(
        posts={"/api/blocker": {"ok": True, "result": {"resolved": True, "say": "Checked.", "state": state}}},
        steps=[{"draw": {"plan_id": "pl_1", "items": [PAGE], "ready": False}}, {"press": "Check again"}],
    )
    sent = next(c for c in out["calls"] if c.startswith("post:/api/blocker"))
    assert json.loads(sent.split(":", 2)[2])["id"] == "page:compliance"
    assert "plan:pl_2" in out["calls"] and "admiry:Checked." in out["calls"]


@needs_node
def test_an_answer_posts_the_question_and_a_refusal_frees_the_row():
    out = _run(
        posts={"/api/blocker": {"ok": False, "error": "Tell me the business phone with its area code"}},
        steps=[{"draw": {"plan_id": "pl_1", "items": [PHONE], "ready": False}}, {"answer": "555"}],
    )
    body = json.loads(next(c for c in out["calls"] if c.startswith("post:/api/blocker")).split(":", 2)[2])
    assert (body["action"], body["id"], body["plan_id"], body["text"]) == ("answer", "phone", "pl_1", "555")
    assert not out["rows"][0]["spent"] and "handle:Tell me the business phone with its area code" in out["calls"]


@needs_node
def test_the_yes_posts_the_plan_the_card_shows_and_says_what_happened():
    out = _run(
        posts={"/api/setup": {"ok": True, "launch": {"say": "Google Search: live."}, "state": {}}},
        steps=[{"confirm": True}],
    )
    body = json.loads(next(c for c in out["calls"] if c.startswith("post:/api/setup")).split(":", 2)[2])
    assert (body["action"], body["plan_id"], body["business_id"]) == ("approve", "pl_1", "biz_1")
    assert "admiry:Google Search: live." in out["calls"] and "turn:state" in out["calls"]


def test_the_old_launch_words_are_gone():
    gone = ("Review and launch", "Admiry takes a second look at the plan", "Ready: Confirm and launch")
    for path in (HOME_JS, AGENT / "templates" / "home.html", AGENT / "static" / "setup.js"):
        text = path.read_text()
        assert not any(w in text for w in gone), path.name


def test_the_promise_is_on_the_card_word_for_word():
    html = (AGENT / "templates" / "home.html").read_text()
    assert PROMISE.replace("hello@admiry.ai", '<a href="mailto:hello@admiry.ai">hello@admiry.ai</a>') in html


def test_the_default_heading_in_home_html_is_never_ready_to_launch():
    assert DEFAULT_HEADING != "Ready to launch"
    assert DEFAULT_HEADING == "Your plan"


def test_the_plan_cards_list_has_its_own_class_apart_from_the_site_cards():
    """L1 Task 5 review (R13, Important): the site card's own, unrelated component (home.html's t-site template,
    static/sites.js) owns the bare `.checklist` (home.css ~421-429); the plan card's list carries `launch-checklist`
    instead, so the two never collide in the page's one stylesheet -- a node test applies no CSS, so only a check
    against the real files pins this."""
    html = (AGENT / "templates" / "home.html").read_text()
    plan = html.split('id="t-plan"')[1].split("</template>")[0]
    ol = re.search(r'<ol [^>]*data-part="checklist"[^>]*>', plan).group(0)
    classes = re.search(r'class="([^"]*)"', ol).group(1).split()
    assert classes == ["launch-checklist"]  # never bare "checklist": the site card's own class
    css = (AGENT / "static" / "home.css").read_text()
    for selector in (
        ".launch-checklist",
        ".launch-checklist li",
        ".launch-checklist li.can-wait",
        ".launch-checklist li.spent",
        ".launch-checklist .problem",
        ".launch-checklist .fix",
    ):
        assert f"{selector} {{" in css, selector


def test_the_plan_cards_ready_state_has_its_own_class_apart_from_the_walls_ready():
    """Residual fix item 1 (founder's report, a visual bug): home.js's drawCheck used to toggle the bare `ready` on
    the plan card once its checklist cleared -- the same class the sign-in wall's "Your plan is ready" line owns
    (`.ready`, home.css ~233), at the same specificity as `.card` and later in the sheet, so the Ready to launch
    card centred and shrank its children, lost its `<hr>` borders and turned plain text 13px green. Fixed the way
    R13 fixed the same kind of collision for `.checklist`: the card's own class, `launch-ready`, apart from the
    wall's. A node test applies no CSS, so only a check against the real files pins this."""
    js = HOME_JS.read_text()
    assert 'n.classList.toggle("launch-ready", Boolean(chk.ready));' in js
    assert 'classList.toggle("ready"' not in js  # never the bare class on the card any more
    css = (AGENT / "static" / "home.css").read_text()
    assert ".launch.checks:not(.launch-ready) .launch-buttons" in css
    assert ".launch.checks:not(.launch-ready) [data-when]" in css
    # ".ready" (a literal substring, so it never matches inside ".launch-ready") appears exactly once in the whole
    # sheet: the wall's own rule, untouched -- no rule in home.css targets the plan card with the bare class.
    hits = [line for line in css.splitlines() if ".ready" in line]
    assert hits == [
        ".ready { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 500; color: var(--ok); }"
    ]


def test_the_launch_sections_aria_label_stays_neutral_through_every_state():
    """Residual fix item 2: home.js never updates the launch section's aria-label, so it stays through Checking,
    the checklist and Ready to launch alike -- it must not claim the founder's forbidden "Ready to launch" the
    whole time a screen reader has it, including while the plan still has things to fix."""
    html = HOME_HTML.read_text()
    assert '<section class="card launch" aria-label="Your plan">' in html
    assert 'aria-label="Ready to launch"' not in html
