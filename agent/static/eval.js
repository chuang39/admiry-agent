// The grading page's save-as-you-go (K5, agent/eval_web.py): every change of a grade, a pause in typing a note and
// leaving the note (the blur a click on Next, Previous or All plans causes: `change`), posts the plan's whole form (same
// origin, the form's own csrf) and says "Saved" or why not. The request is `keepalive`, so a click that leaves the page
// right after a note was typed does not cancel the save. Without this script the form's own button saves and moves on.
// One request at a time: every save carries the whole form, so two in the air at once could land in the other order
// and put a note back to earlier text. A change made while one is in the air waits for it (`dirty`) and goes in one more
// save, read when it is sent; "Saved" is said only when nothing is left to send. The button, pressed while saves are
// pending, waits for them and then posts the form itself.
(function () {
  "use strict";
  var form = document.getElementById("grades");
  var status = document.getElementById("save-status");
  if (!form || !status || !window.fetch || !window.FormData) return;
  var timer = null; // a pause in typing, waiting to save
  var inFlight = false; // a request is in the air: no other is sent until it lands
  var dirty = false; // something changed since the request in the air was built
  var submitAfter = false; // the button was pressed while saves were pending
  function save() {
    clearTimeout(timer);
    timer = null;
    if (inFlight) {
      dirty = true;
      return;
    }
    inFlight = true;
    dirty = false;
    status.textContent = "Saving…";
    var said = "";
    fetch(form.action, {
      method: "POST",
      body: new FormData(form), // the form as it is now
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      keepalive: true, // outlives the page: a link clicked right after typing a note does not drop its save
    })
      .then(function (r) {
        return r.ok ? r.json() : Promise.reject(r.status);
      })
      .then(function (d) {
        said = "Saved: " + d.graded + " of " + d.dimensions + " graded";
      })
      .catch(function (e) {
        said = "Not saved (" + e + "): use the button";
      })
      .then(function () {
        inFlight = false;
        if (dirty) {
          save(); // what changed meanwhile, one save, whatever came of this one
        } else if (timer !== null) {
          status.textContent = "Saving…"; // a note waits for its pause
        } else {
          status.textContent = said;
          if (submitAfter) {
            submitAfter = false;
            form.submit();
          }
        }
      });
  }
  form.addEventListener("change", function (e) {
    // a grade at once; a note when the founder leaves it (save() takes the pause's timer with it, so no second save)
    if (e.target.type === "radio" || e.target.tagName === "TEXTAREA") save();
  });
  form.addEventListener("input", function (e) {
    if (e.target.tagName !== "TEXTAREA") return;
    clearTimeout(timer);
    status.textContent = "Saving…";
    timer = setTimeout(save, 800);
  });
  form.addEventListener("submit", function (e) {
    if (!inFlight && !dirty && timer === null) return; // nothing pending: the form posts itself
    e.preventDefault();
    submitAfter = true;
    if (timer !== null) save();
  });
})();
