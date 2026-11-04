"""site.js's behaviour under Node with a stub DOM (spec 2026-09-29-admiry-settings-redesign §4, §6): the section nav
marks the section in view, and the header's "soon" links go nowhere. The look is checked in a browser by hand; the
choice of entry is pinned here, which a test of the file's strings cannot do."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="drives site.js with node")
SITE_JS = Path(__file__).parents[1] / "static" / "site.js"
IDS = ["overview", "ads", "assistants", "advanced", "account"]

# A stub of what site.js touches, then the steps: ["io", {id: in the band?, ...}] delivers one observer batch (in
# page order, as the spec queues them), ["scroll", y] moves the window, ["nav", id] clicks a nav entry, ["scrollend"]
# ends a jump, ["soon"] clicks a header link marked aria-disabled. Prints the marked entry after each step (for
# "soon": whether the click was cancelled).
HARNESS = r"""
const src = require("fs").readFileSync(process.argv[1], "utf8");
const [ids, steps] = process.argv.slice(2).map(JSON.parse);
const on = {}, add = (k) => (t, f) => (on[k + t] ||= []).push(f), fire = (k, ev) => (on[k] || []).forEach((f) => f(ev));
let current = null, observe = null;
const links = ids.map((id) => ({ id, getAttribute: () => "#" + id, setAttribute: () => { current = id; },
  removeAttribute: () => { if (current === id) current = null; } }));
const nav = { querySelectorAll: () => links, addEventListener: add("nav:") };
const sections = Object.fromEntries(ids.map((id) => [id, { id, tagName: "SECTION" }]));
global.window = { scrollY: 0, innerHeight: 950, confirm: () => true, addEventListener: add("win:") };
global.IntersectionObserver = window.IntersectionObserver = class { constructor(cb) { observe = cb; } observe() {} };
global.location = { hash: "" };
global.navigator = {};
global.document = { documentElement: { scrollHeight: 3000 }, addEventListener: add("doc:"),
  querySelectorAll: (s) => (s === "nav[data-sections]" ? [nav] : []),
  querySelector: (s) => (s === "nav[data-sections]" ? nav : null), getElementById: (id) => sections[id] || null };
eval(src);
fire("doc:DOMContentLoaded");
const out = steps.map(([kind, arg]) => {
  if (kind === "io") observe(Object.entries(arg).map(([id, hit]) => ({ target: sections[id], isIntersecting: hit })));
  if (kind === "scroll") { window.scrollY = arg; fire("win:scroll"); }
  if (kind === "scrollend") fire("win:scrollend");
  if (kind === "nav") fire("nav:click", { target: { closest: () => links[ids.indexOf(arg)] } });
  if (kind === "soon") {
    let cancelled = false;
    const a = { getAttribute: () => "#" };
    fire("doc:click", { target: { closest: () => a }, preventDefault: () => { cancelled = true; } });
    return cancelled;
  }
  return current;
});
console.log(JSON.stringify(out));
"""


def _run(steps: list) -> list:
    out = subprocess.run(
        ["node", "-e", HARNESS, str(SITE_JS), json.dumps(IDS), json.dumps(steps)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(out.stdout)


def _band(*ids: str) -> dict:
    return {i: i in ids for i in IDS}


def test_on_load_the_topmost_section_in_the_band_is_marked():
    """A short Overview on a tall window: Overview and Ads are both in the band at the top of the page, and every
    section reports in the first batch, in page order. Overview is what the owner is looking at."""
    assert _run([["io", _band("overview", "ads")]]) == ["overview"]


def test_a_section_that_stays_in_the_band_is_marked_when_the_one_below_leaves():
    """Scrolling back up a little: the lower of two sections in the band leaves through the bottom and sends the
    only entry; the upper one, which stayed, is marked (not the one that left)."""
    marks = _run(
        [
            ["io", _band("overview", "ads")],
            ["scroll", 300],
            ["io", {"overview": False}],  # Overview leaves through the top
            ["io", {"assistants": True}],  # Assistants comes in below Ads
            ["scroll", 280],
            ["io", {"assistants": False}],  # back up: Assistants leaves through the bottom
        ]
    )
    assert marks == ["overview", "overview", "ads", "ads", "ads", "ads"]


def test_the_page_end_marks_the_last_entry_and_leaving_it_marks_the_band_again():
    marks = _run([["io", _band("advanced")], ["scroll", 3000 - 950], ["scroll", 1900]])
    assert marks == ["advanced", "account", "advanced"]


def test_a_clicked_entry_holds_through_the_jump_until_the_owner_scrolls_away():
    marks = _run(
        [
            ["io", _band("overview")],
            ["nav", "advanced"],
            ["io", {"overview": False, "ads": True}],  # the jump passes Ads through the band
            ["scrollend"],  # landed (at scrollY 0 in the stub)
            ["scroll", 30],
            ["scroll", 60],  # more than 40px from where the jump landed
        ]
    )
    assert marks == ["overview", "advanced", "advanced", "advanced", "advanced", "ads"]


def test_the_headers_soon_links_go_nowhere():
    """Insights (aria-disabled): site.js, loaded by the home and Settings, cancels the click; "Your ads" is a real
    link since YA."""
    assert _run([["soon"]]) == [True]
