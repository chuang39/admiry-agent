"""eval.js's save-as-you-go under Node with a stub page (K5 Task 10 review): the grading page saves the whole form, so two
saves in the air at once -- a note's pause and a grade's click -- could land in the other order on the server and put the
founder's note back to earlier text while the page said "Saved". One request at a time: a change made while one is in the
air waits for it and goes in one more save, read when it is sent; "Saved" is said only when nothing is left to send. The
look is checked in a browser by hand; the order of the requests is pinned here, which a test of the file's strings
cannot do. A note typed less than 800 ms before the founder follows a link (Next, Previous, All plans) is saved by the
textarea's `change` (the blur the click causes), in a request that survives the navigation (`keepalive`) (K5 final
review)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="drives eval.js with node")
EVAL_JS = Path(__file__).parents[1] / "static" / "eval.js"

# A stub of what eval.js touches (the form, the status line, FormData, fetch, timers on a fake clock), then the steps:
# ["radio", grade] picks a grade, ["type", text] types a note, ["blur"] leaves the note (the `change` a browser fires on
# the textarea when a click on a link moves focus away), ["advance", ms] moves the clock, ["settle", n, code] answers
# the n-th request (200 saved, else that error), ["submit"] presses the button. FormData reads the form's values when it
# is built, as a browser does, so a request's `sent` is the form as it was at the moment it was sent. Prints, after each
# step, the number of requests sent, the status line, the number of programmatic submits and (for "submit") whether the
# click was cancelled -- and, at the end, every request's form, its `keepalive` option and the most requests ever in
# the air together.
HARNESS = r"""
const src = require("fs").readFileSync(process.argv[1], "utf8");
const steps = JSON.parse(process.argv[2]);
let clock = 0, nextId = 1, timers = [];
global.setTimeout = (f, ms) => { const id = nextId++; timers.push({ id, f, at: clock + ms }); return id; };
global.clearTimeout = (id) => { timers = timers.filter((t) => t.id !== id); };
const advance = (ms) => {
  const end = clock + ms;
  for (;;) {
    const due = timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at)[0];
    if (!due) break;
    timers = timers.filter((t) => t !== due);
    clock = due.at;
    due.f();
  }
  clock = end;
};
const form = { action: "/ops/eval/plan/k", values: { note: "", grade: "" }, on: {}, submitted: 0,
  addEventListener(type, f) { (this.on[type] ||= []).push(f); }, submit() { this.submitted++; } };
const status = { textContent: "" };
const requests = [];
let inFlight = 0, maxInFlight = 0;
class FormData { constructor(f) { this.sent = JSON.stringify(f.values); } }
global.FormData = FormData;
global.fetch = (url, opts) => {
  const req = { url, sent: opts.body.sent, keepalive: opts.keepalive === true };
  requests.push(req);
  inFlight++;
  maxInFlight = Math.max(maxInFlight, inFlight);
  return new Promise((resolve) => {
    req.settle = (code) => {
      inFlight--;
      resolve(code === 200 ? { ok: true, json: async () => ({ graded: 2, dimensions: 6 }) } : { ok: false, status: code });
    };
  });
};
global.window = { fetch: global.fetch, FormData };
global.document = { getElementById: (id) => (id === "grades" ? form : id === "save-status" ? status : null) };
eval(src);
const fire = (type, ev) => {
  const e = { preventDefault() { this.prevented = true; }, ...ev };
  (form.on[type] || []).forEach((f) => f(e));
  return e;
};
(async () => {
  const log = [];
  for (const [kind, a, b] of steps) {
    let cancelled = null;
    if (kind === "radio") { form.values.grade = a; fire("change", { target: { type: "radio" } }); }
    if (kind === "type") { form.values.note = a; fire("input", { target: { tagName: "TEXTAREA" } }); }
    if (kind === "blur") fire("change", { target: { tagName: "TEXTAREA", type: "textarea" } });
    if (kind === "advance") advance(a);
    if (kind === "settle") { requests[a].settle(b); await new Promise((r) => setImmediate(r)); }
    if (kind === "submit") cancelled = fire("submit", { target: form }).prevented === true;
    log.push({ requests: requests.length, status: status.textContent, submitted: form.submitted, cancelled });
  }
  console.log(JSON.stringify({ log, sent: requests.map((r) => JSON.parse(r.sent)), keepalive: requests.map((r) => r.keepalive), maxInFlight }));
})();
"""


def _run(steps: list) -> dict:
    out = subprocess.run(
        ["node", "-e", HARNESS, str(EVAL_JS), json.dumps(steps)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(out.stdout)


def test_a_grade_saves_at_once_and_says_saved_when_the_server_has_it():
    out = _run([["radio", "4"], ["settle", 0, 200]])
    assert out["log"][0]["requests"] == 1 and out["log"][0]["status"] == "Saving…"
    assert out["log"][1]["status"] == "Saved: 2 of 6 graded"
    assert out["sent"] == [{"note": "", "grade": "4"}]


def test_a_note_saves_after_a_pause_in_typing_not_on_every_key():
    out = _run([["type", "pro"], ["advance", 500], ["type", "promises"], ["advance", 799], ["advance", 1]])
    assert [s["requests"] for s in out["log"]] == [0, 0, 0, 0, 1]
    assert out["sent"] == [{"note": "promises", "grade": ""}]


def test_changes_made_while_a_save_is_in_the_air_wait_for_it_and_go_in_one_more_save_of_the_form_as_it_then_is():
    out = _run(
        [
            ["type", "first draft"],
            ["advance", 800],  # request 0 is sent with the first draft
            ["type", "the final words"],
            ["advance", 800],  # the pause ends with request 0 still in the air: nothing is sent
            ["radio", "2"],  # a grade, also while it is in the air
            ["settle", 0, 200],  # request 0 lands: ONE more save follows, of the form as it is now
            ["settle", 1, 200],
        ]
    )
    assert [s["requests"] for s in out["log"]] == [0, 1, 1, 1, 1, 2, 2]
    assert out["maxInFlight"] == 1  # never two in the air, so none can land after a later one
    assert out["sent"] == [
        {"note": "first draft", "grade": ""},
        {"note": "the final words", "grade": "2"},  # read when it was sent, not when the change was made
    ]
    saying = [s["status"] for s in out["log"]]
    assert saying[5] == "Saving…"  # request 0 landed but its save is not the last: not "Saved" yet
    assert saying[6] == "Saved: 2 of 6 graded"


def test_many_changes_in_the_air_are_one_more_save_not_one_each():
    out = _run([["radio", "1"], ["radio", "2"], ["radio", "3"], ["radio", "4"], ["settle", 0, 200], ["settle", 1, 200]])
    assert out["log"][-1]["requests"] == 2 and out["maxInFlight"] == 1
    assert out["sent"][1] == {"note": "", "grade": "4"}


def test_a_note_still_waiting_for_its_pause_is_not_called_saved():
    out = _run([["radio", "5"], ["type", "late note"], ["settle", 0, 200], ["advance", 800], ["settle", 1, 200]])
    assert out["log"][2]["status"] == "Saving…" and out["log"][2]["requests"] == 1  # request 0 landed, the note waits
    assert out["log"][3]["requests"] == 2  # the pause ends with nothing in the air: sent
    assert out["sent"][1] == {"note": "late note", "grade": "5"}
    assert out["log"][4]["status"] == "Saved: 2 of 6 graded"


def test_a_failed_save_says_so_and_the_next_change_saves_again():
    out = _run([["radio", "3"], ["settle", 0, 500], ["radio", "4"], ["settle", 1, 200]])
    assert out["log"][1]["status"] == "Not saved (500): use the button"
    assert out["log"][2]["requests"] == 2 and out["log"][3]["status"] == "Saved: 2 of 6 graded"


def test_the_button_pressed_while_a_save_is_in_the_air_waits_for_it_then_posts_the_form_itself():
    out = _run([["radio", "3"], ["submit"], ["settle", 0, 200]])
    assert out["log"][1]["cancelled"] is True and out["log"][1]["submitted"] == 0  # held back: a save is in the air
    assert out["log"][2]["submitted"] == 1 and out["maxInFlight"] == 1


def test_the_button_pressed_while_a_note_waits_for_its_pause_sends_it_first():
    out = _run([["type", "note"], ["submit"], ["settle", 0, 200]])
    assert out["log"][1]["cancelled"] is True and out["log"][1]["requests"] == 1  # the note goes now, not after 800 ms
    assert out["log"][2]["submitted"] == 1 and out["sent"] == [{"note": "note", "grade": ""}]


def test_a_note_typed_just_before_leaving_by_a_link_is_saved_on_the_blur_not_lost_to_the_pause():
    out = _run([["type", "last words"], ["advance", 200], ["blur"], ["advance", 2000]])
    assert [s["requests"] for s in out["log"]] == [0, 0, 1, 1]  # saved by the blur, 600 ms before the pause ends
    assert out["sent"] == [{"note": "last words", "grade": ""}]  # the pause's timer was cancelled: no second save
    out = _run([["type", "last words"], ["blur"], ["settle", 0, 200], ["advance", 2000]])
    assert out["log"][2]["status"] == "Saved: 2 of 6 graded" and out["log"][3]["requests"] == 1


def test_every_save_asks_to_survive_the_page_being_left():
    out = _run([["radio", "4"], ["settle", 0, 200], ["type", "n"], ["advance", 800], ["settle", 1, 200]])
    assert len(out["sent"]) == 2 and out["keepalive"] == [True, True]  # a grade's and a pause's save alike
    out = _run([["type", "n"], ["blur"]])
    assert out["keepalive"] == [True]  # and the blur's: the one the navigation interrupts


def test_a_blur_while_a_save_is_in_the_air_still_waits_for_it_and_goes_in_one_more_save():
    out = _run([["radio", "3"], ["type", "the note"], ["blur"], ["settle", 0, 200]])
    assert [s["requests"] for s in out["log"]] == [1, 1, 1, 2] and out["maxInFlight"] == 1  # none while one is up
    assert out["sent"] == [{"note": "", "grade": "3"}, {"note": "the note", "grade": "3"}]
    out = _run(
        [["radio", "3"], ["type", "the note"], ["blur"], ["settle", 0, 200], ["settle", 1, 200], ["advance", 2000]]
    )
    assert out["log"][4]["status"] == "Saved: 2 of 6 graded" and out["log"][5]["requests"] == 2


def test_the_button_with_nothing_in_the_air_is_the_forms_own_post():
    out = _run([["radio", "3"], ["settle", 0, 200], ["submit"]])
    assert out["log"][2]["cancelled"] is False and out["log"][2]["submitted"] == 0
