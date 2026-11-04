/* The Help button (spec 2026-10-12 §3): the pill bottom right and the card it opens, on every page an owner or a
   visitor reads (agent/templates/_help.html). Open; close by Cancel, Close, the backdrop, Escape or Done (focus goes
   back to the pill); send to POST /api/help; three states: closed, open, sent. A visitor with no web session types an
   address: the field shows when GET /api/csrf answers no token (the server decides who is writing either way). No
   other script depends on this one, and it depends on none: it has its own fetch helpers, so the plain pages (which
   never load support.js) need nothing else. Dynamic strings go in with textContent only. */
"use strict";
(() => {
  const doc = document;
  const $ = (id) => doc.getElementById(id);
  const root = $("help"), pill = $("help-pill"), card = $("help-card"), form = $("help-form");
  const text = $("help-text"), emailRow = $("help-email-row"), email = $("help-email"), error = $("help-error");
  const send = $("help-send"), sent = $("help-sent"), sentText = $("help-sent-text"), done = $("help-done");
  if (!root || !pill || !card || !form) return;
  const PLAN = /^\/plan\/(pl_[0-9a-f]{16})$/; // the plan page: the plan it shows; the homepage's is the server's
  const FAILED = "Something went wrong on our side. Try again soon.";
  const OFFLINE = "I could not reach Admiry. Check your connection and try again.";
  let token = null, asked = false;

  async function csrf() { // a token = a web session: Admiry already has the address (the server reads it again)
    if (asked) return token;
    asked = true;
    try {
      token = (await (await fetch("/api/csrf", { credentials: "same-origin" })).json()).csrf || null;
    } catch (e) { token = null; }
    return token;
  }
  async function post(payload) {
    try {
      const r = await fetch("/api/help", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...payload, csrf: token }) });
      const j = await r.json();
      return j && typeof j === "object" ? j : { ok: false, error: FAILED };
    } catch (e) {
      return { ok: false, error: OFFLINE };
    }
  }
  function show(message) {
    error.textContent = message || "";
    error.hidden = !message;
  }
  async function open() {
    const signedIn = Boolean(await csrf());
    emailRow.hidden = signedIn;
    email.required = !signedIn;
    root.classList.add("open");
    card.hidden = false;
    pill.setAttribute("aria-expanded", "true");
    if (sent.hidden) text.focus(); else done.focus();
  }
  function close() {
    root.classList.remove("open");
    card.hidden = true;
    pill.setAttribute("aria-expanded", "false");
    pill.focus();
  }
  function reset() { // Done: the form again, empty, for the next report
    form.hidden = false;
    sent.hidden = true;
    text.value = "";
    show(null);
  }
  pill.addEventListener("click", () => (card.hidden ? open() : close()));
  $("help-cancel").addEventListener("click", close);
  $("help-close").addEventListener("click", close);
  $("help-backdrop").addEventListener("click", close);
  done.addEventListener("click", () => (reset(), close()));
  doc.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !card.hidden) close();
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const words = text.value.trim();
    if (!words) return text.focus();
    const typed = email.value.trim();
    if (!emailRow.hidden && !typed) return email.focus();
    send.disabled = true;
    show(null);
    const page = location.pathname, plan = PLAN.exec(page);
    const payload = { text: words, page, plan_id: plan ? plan[1] : null };
    if (!emailRow.hidden) payload.email = typed;
    const r = await post(payload);
    send.disabled = false;
    if (!r.ok) return show(r.error || (typeof r.detail === "string" ? r.detail : null) || FAILED); // the words stay in the box for another try
    sentText.textContent = "A person at Admiry will write back to " + r.sent_to + ".";
    form.hidden = true;
    sent.hidden = false;
    done.focus();
  });
})();
