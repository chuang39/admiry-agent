/* The plan thumbs (spec 2026-09-30 support-and-feedback §6). The homepage loads this when AGENT_SUPPORT is on (home.js
   mounts the thumbs under each plan version); /plan/{id} loads it for a sampled plan (its host carries data-rate-plan).
   "Talk to a person" left with the Help button (spec 2026-10-12 H4: static/help.js, on every page). Dynamic strings go
   in with textContent only. */
"use strict";
window.AdmirySupport = (() => {
  const doc = document;
  const el = (tag, cls, text) =>
    Object.assign(doc.createElement(tag), cls ? { className: cls } : {}, text != null ? { textContent: text } : {});
  const REASONS = [ // feedback.REASONS, in order
    ["budget", "Budget doesn't fit"], ["area", "Wrong area or customers"], ["copy", "Ads don't sound like us"],
    ["business", "Didn't understand my business"], ["other", "Something else"],
  ];
  let token = null;

  async function csrf() {
    if (token) return token;
    try {
      token = (await (await fetch("/api/csrf", { credentials: "same-origin" })).json()).csrf || null;
    } catch (e) { token = null; }
    return token;
  }
  async function post(path, payload) {
    try {
      const r = await fetch(path, { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...payload, csrf: await csrf() }) });
      const j = await r.json();
      return j && typeof j === "object" ? j : { ok: false, error: "Something went wrong on our side. Try again soon." };
    } catch (e) {
      return { ok: false, error: "I could not reach Admiry. Check your connection and try again." };
    }
  }

  // ---------------------------------------------------------------- the plan thumbs (the homepage and /plan)
  function rate(host, { planId, surface, vote }) {
    if (!planId) return;
    const box = el("section", "rate"), row = el("div", "rate-row");
    box.setAttribute("aria-label", "Rate this plan");
    const up = Object.assign(el("button", "rate-b", "👍"), { type: "button" });
    const down = Object.assign(el("button", "rate-b", "👎"), { type: "button" });
    up.setAttribute("aria-label", "Useful");
    down.setAttribute("aria-label", "Not useful");
    row.append(el("p", "rate-q", "Was this plan useful?"), up, down);
    const note = el("p", "rate-note");
    note.setAttribute("role", "status");
    const why = el("form", "rate-why");
    why.hidden = true;
    const boxes = REASONS.map(([id, text]) => {
      const c = Object.assign(el("input"), { type: "checkbox", value: id, id: `rate-${planId}-${id}` });
      const l = el("label", "rate-r");
      l.htmlFor = c.id;
      l.append(c, doc.createTextNode(` ${text}`));
      why.append(l);
      return c;
    });
    const comment = Object.assign(el("textarea"), { rows: 2, maxLength: 1000, placeholder: "Anything else? (optional)" });
    comment.setAttribute("aria-label", "Anything else");
    const contact = Object.assign(el("input"), { type: "checkbox", id: `rate-${planId}-contact` });
    const cl = el("label", "rate-r");
    cl.htmlFor = contact.id;
    cl.append(contact, doc.createTextNode(" Have someone from Admiry contact me"));
    why.append(comment, cl, Object.assign(el("button", "btn", "Send"), { type: "submit" }));
    const mark = (rating) => {
      up.setAttribute("aria-pressed", String(rating === "up"));
      down.setAttribute("aria-pressed", String(rating === "down"));
    };
    async function submit(rating) {
      const d = rating === "down";
      const r = await post("/api/feedback", {
        plan_id: planId, surface, rating,
        reasons: d ? boxes.filter((c) => c.checked).map((c) => c.value) : [],
        comment: d ? comment.value.trim() || null : null,
        contact_me: d && contact.checked,
      });
      if (!r.ok) return (note.textContent = r.error || r.detail || "Something went wrong.");
      mark(rating);
      why.hidden = true;
      note.textContent = r.ticket_id ? "Thanks — noted. The Admiry team will reach out by e-mail." : "Thanks — noted.";
    }
    up.addEventListener("click", () => submit("up"));
    down.addEventListener("click", () => ((why.hidden = false), boxes[0].focus()));
    why.addEventListener("submit", (e) => (e.preventDefault(), submit("down")));
    if (vote) {
      mark(vote.rating);
      boxes.forEach((c) => (c.checked = (vote.reasons || []).includes(c.value)));
      comment.value = vote.comment || "";
      note.textContent = "Thanks — noted.";
    }
    box.append(row, why, note);
    host.append(box);
  }

  doc.querySelectorAll("[data-rate-plan]").forEach((host) => {
    let vote = null;
    try { vote = JSON.parse(host.dataset.vote || "null"); } catch (e) { vote = null; }
    rate(host, { planId: host.dataset.ratePlan, surface: host.dataset.surface || "plan_page", vote });
  });
  return { rate };
})();
