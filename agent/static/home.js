/* The homepage (spec 2026-09-26 §3): one conversation over POST /api/turn. No framework and nothing loaded
   from elsewhere; every dynamic string goes in with textContent, and every moment after the first visit (the
   wall, the plan, the assistant sheet) is a <template> in home.html cloned here. */
"use strict";
(() => {
  const doc = document;
  const $ = (id) => doc.getElementById(id);
  const hero = $("hero"), convo = $("convo"), log = $("log"), dock = $("dock"), reply = $("reply");
  const ask = $("ask"), live = $("live"), foot = $("foot"), composer = $("composer");
  const PROMPTS = JSON.parse($("prompts").textContent);
  const LEVELS = ["minimum", "conservative", "recommended", "aggressive"]; // tools_consult.LEVEL_NAMES, cheapest first
  const DAYS = 30; // plans.DAYS: a month of budget is DAYS × the daily amount
  const BUILD = { id: "recommend", label: "Build my plan", tool: "recommend", primary: true };
  const RESULTS = { consultation_booking: "booking", phone_call_60s: "a call over 60\u00a0s", form_submit: "a form sent",
    page_view: "a page visit" };
  const ASSISTANTS = { muse: "Muse", claude: "Claude", chatgpt: "ChatGPT" };
  const MUSE_STORE = { ios: "https://apps.apple.com/us/app/muse-from-meta/id6760173601", web: "https://muse.ai",
    android: "https://play.google.com/store/apps/details?id=com.facebook.aura" };
  const PLAN_INTRO =
    "Here is the plan. Every number below is a planning estimate you can change, and I show where each one comes from.";
  const HANDOFF_KEY = "admiry:start"; // website/components/marketing/composer-focus.ts writes it
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const returning = doc.body.classList.contains("returning"); // read before the load clears it (a fresh start's line)
  const S = {
    signedIn: doc.body.dataset.signedIn === "1", claimed: false, wall: false, claimTried: false, csrf: null,
    sessionId: null, status: null /* open | ready | planned */, question: null, progress: null,
    cards: null, planId: null, planUrl: null, next: null, launchNote: null, planNode: null, businessId: null,
    busy: false, handoff: null, sheet: null, tab: "muse", planStatus: null, inReview: null,
  };

  // ------------------------------------------------------------------ small helpers
  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID(); // secure contexts only; plain-http dev falls back
    return Array.from(crypto.getRandomValues(new Uint8Array(16)), (x) => x.toString(16).padStart(2, "0")).join("");
  }
  const clone = (id) => $(id).content.firstElementChild.cloneNode(true);
  const part = (root, name) => root.querySelector(`[data-part="${name}"]`);
  const el = (tag, cls, text) =>
    Object.assign(doc.createElement(tag), cls ? { className: cls } : {}, text != null ? { textContent: text } : {});
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
  const usd = (n) => "$" + Math.round(Number(n)).toLocaleString("en-US");
  const usdShort = (n) => (n >= 1000 ? "$" + Math.round(n / 100) / 10 + "k" : usd(n));
  const span = (pair, fmt = String) => (pair[0] === pair[1] ? fmt(pair[0]) : `${fmt(pair[0])} to ${fmt(pair[1])}`);
  const sentence = (s) => ((s = cap(String(s || "").trim())) ? (/[.!?]$/.test(s) ? s : s + ".") : "Something went wrong.");
  const waitWords = (sec) =>
    sec < 60 ? `${sec} second${sec === 1 ? "" : "s"}` : `about ${Math.ceil(sec / 60)} minute${sec <= 60 ? "" : "s"}`;
  const reveal = (node, block = "nearest") => node.scrollIntoView({ block, behavior: reduced ? "auto" : "smooth" });
  const announce = (text) => ((live.textContent = ""), setTimeout(() => (live.textContent = text), 60));
  const safeHref = (url) =>
    typeof url === "string" && (/^https?:\/\//.test(url) || /^\/(?!\/)/.test(url)) ? url : null;

  // ------------------------------------------------------------------ the API
  async function post(path, payload, form) { // form: Settings' own form routes (/connect/ads/…), answered in JSON
    let r, j = null;
    try {
      // follow-up R1: the business this page shows, refused by the server if the cookie's is another
      r = await fetch(path, { method: "POST", credentials: "same-origin",
        headers: { ...(form ? { Accept: "application/json" } : { "Content-Type": "application/json" }), ...(S.businessId ? { "X-Admiry-Business": S.businessId } : {}) },
        body: form ? new URLSearchParams(payload) : JSON.stringify(payload) });
    } catch (e) {
      return { ok: false, error: "I could not reach Admiry. Check your connection and try again." };
    }
    if (r.status === 429) {
      const sec = Math.max(1, parseInt(r.headers.get("Retry-After"), 10) || 60);
      return { ok: false, error: `That was a lot at once, so give it ${waitWords(sec)} and try again.` };
    }
    try { j = await r.json(); } catch (e) { j = null; } // a proxy's error page, not ours
    if (!j || typeof j !== "object") return { ok: false, error: "Something went wrong on our side. Try again soon." };
    if (!("ok" in j)) return { ok: false, error: sentence(j.detail || j.error) }; // an HTTPException (403 csrf, …)
    return j;
  }
  async function get(path) { // a read under post's rules (follow-up R1's header), answered in JSON
    let j = null;
    try {
      const r = await fetch(path, { credentials: "same-origin", headers: S.businessId ? { "X-Admiry-Business": S.businessId } : {} });
      j = await r.json();
    } catch (e) {
      return { ok: false, error: "I could not reach Admiry. Check your connection and try again." };
    }
    return j && typeof j === "object" && "ok" in j ? j : { ok: false, error: "Something went wrong on our side. Try again soon." };
  }
  const turn = (action, extra) => post("/api/turn", { action, idempotency_key: uuid(), ...extra });
  async function csrf() {
    try {
      S.csrf = S.csrf || (await (await fetch("/api/csrf", { credentials: "same-origin" })).json()).csrf || null;
    } catch (e) { S.csrf = null; }
    return S.csrf;
  }
  function visitor(v) {
    if (!v) return;
    Object.assign(S, { signedIn: Boolean(v.signed_in), claimed: Boolean(v.claimed), wall: Boolean(v.wall) });
    if (v.session_id) S.sessionId = v.session_id;
    if (v.business_id) S.businessId = v.business_id; // whose page this is (R1)
  }

  // ------------------------------------------------------------------ busy, moments
  const CONTROLS = ".plan button, .plan input, .question:not(.spent) button, .question:not(.spent) input, .actions button, .launch-checklist button, .launch-checklist input";
  function setBusy(on) {
    S.busy = on;
    doc.body.classList.toggle("busy", on);
    convo.setAttribute("aria-busy", String(on));
    $("thinking").hidden = !on;
    for (const c of [reply, $("send"), ...doc.querySelectorAll(CONTROLS)]) c.disabled = on;
  }
  async function work(fn) {
    if (S.busy) return;
    const typing = doc.activeElement === reply;
    setBusy(true);
    try { await fn(); } finally {
      setBusy(false);
      if (typing && (!doc.activeElement || doc.activeElement === doc.body)) reply.focus();
    }
  }
  function moment(talking) {
    hero.hidden = foot.hidden = talking;
    convo.hidden = dock.hidden = !talking;
    composer.hidden = false; // with every moment: only the setup link's card-first pages hide it (setupLink, underCard)
    if (talking) grow(); // sized to its placeholder, which takes two lines on a 320px phone
    doc.body.classList.toggle("talking", talking);
  }
  function firstVisit(prefill, reason) { // the log goes, and the setup card that lived in it (S.setup)
    Object.assign(S, { sessionId: null, status: null, cards: null, planNode: null, progress: null, question: null,
      setup: null, businessId: null });
    doc.querySelectorAll(".notice").forEach((p) => (p.hidden = p.dataset.reason !== reason)); // fixed copy, home.html
    log.replaceChildren();
    moment(false);
    if (prefill) ask.value = prefill;
  }

  // ------------------------------------------------------------------ the transcript
  const add = (node) => (log.append(node), node);
  function you(text, chip) {
    const n = clone(chip ? "t-you-chip" : "t-you"), t = part(n, "text");
    if (chip) t.append(clone("t-check"));
    t.append(doc.createTextNode(text));
    return add(n);
  }
  function admiry(text, cls) {
    const n = clone("t-admiry");
    part(n, "body").append(el("p", cls ? `msg ${cls}` : "msg", text));
    announce(text);
    return add(n);
  }
  const oops = (text) => admiry(sentence(text), "error");
  // `say` ends with the question as one sentence for assistants that show only text; the page shows the card. The
  // sentence as consult.question_sentence writes it: an open question (no options), or its options (option_labels).
  function withoutQuestion(say, q) {
    const pick = q.input === "multi" ? "pick up to three, or answer" : "or answer";
    const sep = q.options.some((o) => o.label.includes(", ")) ? "; " : ", ";
    const tail = q.options.length
      ? `${q.prompt} Options: ${q.options.map((o) => o.label).join(sep)} — ${pick} in your own words.`
      : `${q.prompt} Answer in your own words.`;
    return say.endsWith(tail) ? say.slice(0, -tail.length).trim() : say;
  }
  async function handle(r, intro) {
    if (r.ok) return r.result ? show(r, intro) : r.choose ? welcome(r.choose) : undefined;
    if (r.fresh) return firstVisit();
    if (r.wall) return (S.wall = true), toWall();
    if (r.signin) return toWall(true);
    if (r.reload) return reloadOffer(r.error);
    oops(r.error);
  }
  const OTHER_PAGE = "This page is showing another business. Reload it to continue."; // as home_api.OTHER_PAGE
  function reloadOffer(text) { // follow-up R1: another tab switched this browser to another business; catch up here
    const n = oops(text), row = el("div", "fix");
    row.append(fixButton("Reload", true, () => location.reload()));
    part(n, "body").append(row);
  }
  function show(r, intro) {
    visitor(r.visitor);
    const env = r.result, d = env.data || {}, next = env.next || { kind: "none" };
    if (env.resolved) return resolved(env);
    if (d.cards) {
      plan(d, next, intro || PLAN_INTRO);
      return;
    }
    const q = next.kind === "question" ? next.question : null;
    let say = q ? withoutQuestion(env.say || "", q) : env.say || "";
    if (S.wall) say = say.replace(/\s*(?:I have what I need; )?say the word and I will build the plan\.$/i, ""); // the wall asks instead
    if (say) admiry(say);
    if (d.reading) readingCard(d.reading);
    if (d.ready) S.status = "ready", progress(d.steps);
    else if (q) S.status = "open";
    else if (next.kind === "actions" && next.actions.some((a) => a.tool === "recommend")) S.status = "ready";
    if (S.wall) return toWall();
    if (q) return question(q);
    if (next.kind === "actions") return actions(next.actions);
    if (next.kind === "link") return link(next.link);
    if (S.status === "ready") actions([BUILD]); // "say the word" needs a word to say
  }
  function question(q) {
    const n = clone("t-question"), group = part(n, "options"), extra = part(n, "extra");
    n.dataset.qid = q.id; // typed words post it (send): after a go_to they answer this question, not the next (K6 C1)
    part(n, "index").textContent = `${q.index} / ${q.count}`;
    part(n, "prompt").textContent = q.prompt;
    group.setAttribute("aria-label", q.prompt);
    const any = q.options.find((o) => o.id === "any"), main = q.options.filter((o) => o !== any);
    // One tap answers a "single" question. A "multi" one toggles its picks and Continue sends them as a list.
    const cards = main.some((o) => o.sublabel), multi = q.input === "multi", picked = new Set();
    const button = (cls, text) => Object.assign(el("button", cls, text), { type: "button" });
    const send = multi && button("btn dark next", "Continue"); // aria-disabled: setBusy re-enables `disabled`
    group.classList.add(cards ? "cards" : "pills");
    for (const o of main) {
      const b = button(cards ? "opt" : "pill", cards ? null : o.label);
      if (cards) b.append(el("b", null, o.label), el("span", null, o.sublabel || ""));
      if (multi) b.setAttribute("aria-pressed", "false");
      b.addEventListener("click", () => {
        if (!multi) return choose(n, q, { choice: o.id }, o.label, b);
        picked.has(o) ? picked.delete(o) : picked.add(o);
        b.classList.toggle("on", picked.has(o)), b.setAttribute("aria-pressed", String(picked.has(o)));
        send.setAttribute("aria-disabled", String(!picked.size));
      });
      group.append(b);
    }
    if (multi) extra.prepend(send), send.setAttribute("aria-disabled", "true"), send.addEventListener("click", () => {
      if (!picked.size) return;
      const ordered = main.filter((o) => picked.has(o)); // in the order shown, not the order tapped
      choose(n, q, { choice: ordered.map((o) => o.id) }, ordered.map((o) => o.label).join(", "), null);
    });
    if (any) {
      const b = button("pill", any.label);
      b.addEventListener("click", () => choose(n, q, { choice: any.id }, any.label, b));
      extra.prepend(b);
    }
    const other = part(n, "other"), input = part(n, "other-input");
    if (q.other && q.other.allowed) {
      input.placeholder = q.other.placeholder || "Something else…";
      other.addEventListener("submit", (e) => {
        e.preventDefault();
        if (input.value.trim()) choose(n, q, { other: input.value.trim() }, input.value.trim(), null);
      });
    } else other.remove();
    if (!extra.children.length) extra.remove();
    S.question = add(n);
    reveal(n);
    (group.querySelector("button") || part(n, "prompt")).focus({ preventScroll: true });
  }
  function readingCard(v) { // what a site's reading gave (spec 2026-10-12 §3.3, R6): the parts present, for the
    // owner to check; the platform's display name is the server's own (consult.reading_view), nothing to map here
    const n = clone("t-reading");
    rows(part(n, "facts"), [["Business", v.name], ["Offers", (v.services || []).join(", ")], ["Area", v.area],
      ["Phone", v.phone], ["Built on", v.platform]]);
    const pages = part(n, "pages");
    if (v.pages) pages.textContent = `Admiry read ${v.pages} page${v.pages === 1 ? "" : "s"}.`, pages.hidden = false;
    add(n);
  }
  function spend(n, on) {
    n.classList.toggle("spent", on);
    n.querySelectorAll("button, input").forEach((c) => (c.disabled = on));
  }
  function choose(n, q, value, label, button) {
    work(async () => {
      spend(n, true);
      if (button) button.classList.add("on");
      const chip = you(label, true);
      const r = await turn("answer", { answers: { [q.id]: value } });
      if (!r.ok) chip.remove(), spend(n, false), button && button.classList.remove("on");
      await handle(r);
    });
  }
  function progress(steps) {
    if (!Array.isArray(steps)) return;
    const n = (S.progress = S.progress && S.progress.isConnected ? S.progress : add(clone("t-progress")));
    const ul = part(n, "steps");
    ul.replaceChildren();
    for (const s of steps) {
      const li = el("li", s.done ? "done" : "todo");
      li.append(s.done ? clone("t-check") : el("span", "ring"), doc.createTextNode(s.label));
      if (!s.done) li.append(el("span", "sr", " (not yet)"));
      ul.append(li);
    }
  }
  function actions(list) {
    const row = add(el("div", "actions"));
    for (const a of list) {
      const b = Object.assign(el("button", a.primary ? "btn dark" : "btn", a.label), { type: "button" });
      b.addEventListener("click", () => act(a));
      row.append(b);
    }
  }
  function link(l) {
    const href = safeHref(l && l.url);
    if (href) add(el("div", "actions")).append(Object.assign(el("a", "btn", l.label || href), { href }));
  }
  function act(a) {
    const tool = a.tool;
    if (tool === "recommend") return work(recommend);
    if (tool === "request_connect_link") return work(toWall);
    if (tool === "adjust") return focusBudget();
    if (a.id === "confirm") return confirmPlan(a); // the owner's yes, once funded or the own account is live (L1 §5)
    // Admiry running it ("prepay": request_payment_link, or connect_channel with mode managed), else the own account
    // -- the card's pay form names the plan (Agree and pay / Agree and launch)
    openSetup(a.id === "prepay" || tool === "request_payment_link" ? "managed" : "own_account", null, false, null, null, true);
  }
  // The ads setup cards (static/setup.js) under the plan, or alone for /?setup=<business> (assistants and Settings
  // link there). A business this owner does not have answers "gone": the cards go and the home carries on.
  function openSetup(pick, businessId = null, paid = false, onGone = null, onSettled = null, agree = false) {
    log.querySelectorAll(".setup-host").forEach((h) => h.remove()); // one at a time: the latest pick wins
    const host = add(el("div", "setup-host"));
    S.setup = window.AdmirySetup.run({ host, businessId, pick, paid, csrf, onSettled, agree,
      page: !businessId && S.claimed ? S.businessId : null, // a card opened under a plan sets up the business its page shows (follow-up R1)
      onGone: () => (host.remove(), (S.setup = null), onGone && onGone()) });
    return reveal(host, "start"), host;
  }

  // ------------------------------------------------------------------ the launch checklist (L1, spec 2026-10-10 §4)
  // Every reason the plan cannot launch yet, at once, on its launch card: Checking while the server reads them
  // (GET /api/launch_check), then one row each -- the problem, what Admiry suggests, its field or its one-tap fix
  // (B0's resolutions) -- and only once nothing blocks it Ready to launch, with the ways to agree and the promise. A fix
  // draws the plan again, so the card checks again; a place that can wait is listed and blocks nothing.
  const INPUTS = { tel: ["tel", "tel", null], address: ["text", "street-address", null], miles: ["text", "off", "decimal"] };
  const OWN_WORDS = new Set(["prepay", "link_own_account"]); // the card's own words for these two (home.html)
  async function checkPlan(n) {
    n.classList.add("checks");
    part(n, "checking").hidden = false;
    part(n, "launch-k").textContent = "Checking your plan"; // never "Ready to launch" before it is: founder's rule
    const planId = S.planId, r = await get("/api/launch_check");
    if (!n.isConnected || S.planId !== planId) return; // a newer plan was drawn meanwhile: its own check runs
    part(n, "checking").hidden = true;
    if (!r.ok) { part(n, "launch-k").textContent = "Your plan"; return handle(r); }
    drawCheck(n, r.check);
  }
  function drawCheck(n, chk) {
    const list = part(n, "checklist");
    list.replaceChildren();
    if (!chk || chk.plan_id !== S.planId) { part(n, "launch-k").textContent = "Your plan"; return; } // no draft any more, or another one: the next draw says it
    const items = chk.items || [], blocking = items.filter((i) => i.blocking).length;
    n.classList.toggle("launch-ready", Boolean(chk.ready)); // the card's own class: apart from the wall's bare .ready
    part(n, "launch-k").textContent = chk.ready ? "Ready to launch"
      : blocking === 1 ? "One thing before it can launch" : `${blocking} things before it can launch`;
    part(n, "promise").hidden = !chk.ready;
    list.hidden = !items.length;
    for (const it of items) list.append(checkRow(it));
    if (!chk.ready) announce(`${blocking} ${blocking === 1 ? "thing" : "things"} to fix before your ads can launch.`);
  }
  function checkRow(it) {
    const li = el("li", it.blocking ? "todo" : "todo can-wait"), body = el("div", "says"), row = el("div", "fix");
    li.dataset.id = it.id;
    body.dataset.part = "body";
    li.append(el("p", "problem", it.text));
    if (it.suggestion) li.append(el("p", "note", it.suggestion));
    if (it.note) li.append(el("p", "note", it.note));
    li.append(body);
    if (it.question) row.append(answerForm(li, it));
    for (const a of it.actions || []) row.append(fixButton(a.label, a.primary, () => fix(li, it, a)));
    if (row.children.length) body.append(row);
    return li;
  }
  function fixButton(label, primary, onClick) {
    const b = Object.assign(el("button", primary ? "btn dark" : "btn", label), { type: "button" });
    b.addEventListener("click", onClick);
    return b;
  }
  function answerForm(n, it) { // the row's question: its field and Save; typed words reach it too (server, B0 §3.3)
    const q = it.question;
    const [type, autocomplete, inputMode] = INPUTS[q.input] || ["text", "off", null];
    const form = el("form", "fix-answer"), input = Object.assign(el("input"), { type, autocomplete, required: true,
      maxLength: 200, placeholder: q.placeholder || "" }, inputMode ? { inputMode } : {});
    input.setAttribute("aria-label", q.placeholder || "Your answer");
    form.append(input, Object.assign(el("button", "btn dark", "Save"), { type: "submit" }));
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = String(input.value || "").trim();
      if (text) return resolve(n, { action: "answer", id: it.id, plan_id: it.plan_id, text }, text);
    });
    return form;
  }
  function fix(n, it, a) { // a button: what Admiry runs (POST /api/blocker), or the card that already does it
    if (a.id === "budget") return budgetChoice(n, a);
    if (a.id === "build_site") return work(() => (S.planNode ? buildPage(S.planNode) : buildHere()));
    if (a.id === "own_site") return ownSite(n);
    if (a.id === "add_funds" || a.id === "prepay") return openSetup("managed");
    if (a.id === "link_own_account") return openSetup("own_account");
    return resolve(n, { action: a.id, id: it.id, plan_id: it.plan_id }, a.label);
  }
  function budgetChoice(n, a) { // a budget that passes: the plan's own change (adjust); the plan drawn again checks again
    return work(async () => {
      spentFix(n);
      const chip = you(a.label, true), r = await turn("adjust", { ...a.args });
      if (!(r.ok && r.result && r.result.data && r.result.data.cards)) return chip.remove(), unspend(n), handle(r);
      visitor(r.visitor);
      plan(r.result.data, r.result.next);
      admiry(`Set: ${a.label}.`);
    });
  }
  function ownSite(n) { // "Use my website": its address, then the ads' page (the site card's own POST /api/page/own-site)
    if (n.querySelector(".fix-own")) return;
    const form = el("form", "fix-answer fix-own"), input = Object.assign(el("input"), { type: "text",
      inputMode: "url", autocomplete: "url", required: true, maxLength: 320, placeholder: "yourbusiness.com" });
    input.setAttribute("aria-label", "Your website's address");
    form.append(input, Object.assign(el("button", "btn dark", "Use it"), { type: "submit" }));
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const url = String(input.value || "").trim();
      if (!url) return;
      return work(async () => {
        const r = await post("/api/page/own-site", { csrf: await csrf(), url });
        if (!r.ok) return handle(r);
        announce(r.say);
        location.reload(); // the plan's "Your website" card and what stands next come with the page, as the site card's
      });
    });
    part(n, "body").append(form);
    input.focus({ preventScroll: true });
  }
  function resolve(n, body, said) { // what Admiry runs for the owner, said as the owner's turn first
    return work(async () => {
      spentFix(n);
      const chip = you(said, true), token = await csrf();
      const r = token ? await post("/api/blocker", { ...body, csrf: token, idempotency_key: uuid() })
        : { ok: false, error: "Sign in first." };
      if (!r.ok) return chip.remove(), unspend(n), handle(r);
      resolved(r.result);
    });
  }
  function resolved(env) { // a fix's answer: the plan as it stands (its card checks again), then what was done
    const st = env.state || {};
    if (st.cards) plan(st, st.next);
    const said = admiry(env.say);
    if (env.publish) publishOffer(said, env.publish); // the headline's preview: publishing it is the owner's own tap
  }
  function spentFix(n) { // a fix on its way, or done: its controls rest
    n.classList.add("spent");
    n.querySelectorAll("button, input").forEach((c) => (c.disabled = true));
  }
  function unspend(n) { // a fix refused: its controls come back
    n.classList.remove("spent");
    n.querySelectorAll("button, input").forEach((c) => (c.disabled = false));
  }
  function publishOffer(n, p) { // the headline's new preview: publishing it is the owner's own tap (spec §8.1)
    const row = el("div", "fix");
    row.append(fixButton("Publish it", true, () => work(async () => {
      const r = await post("/api/page/publish", { csrf: await csrf(), token: p.token });
      spentFix(n); // published, or refused (the preview ended, the photos changed since): this offer is done
      if (!r.ok) await handle(r);
      else admiry(r.say || "Published."), S.planNode && loadPageState(S.planNode);
      await recheck(); // what stands next -- after a refusal, the fix drawn again
    })));
    part(n, "body").append(row);
  }
  async function recheck() { // after a change made elsewhere on the page (a page published): the plan as it stands
    const r = await turn("state");
    if (r.ok && r.result && r.result.cards) plan(r.result, r.result.next);
    if (S.setup) S.setup.refresh();
  }
  function confirmPlan(a) { // the yes once the money is in or the own account is live (L1 §5)
    return work(async () => {
      const chip = you(a.label, true), token = await csrf();
      const r = token ? await post("/api/setup", { action: "approve", plan_id: S.planId, csrf: token,
        ...(S.businessId ? { business_id: S.businessId } : {}) }) : { ok: false, error: "Sign in first." };
      if (!r.ok) return chip.remove(), oops(r.error);
      admiry((r.launch && r.launch.say) || (r.state && r.state.in_review) || "Your plan is approved.");
      await recheck();
    });
  }
  function reviewing(n) { // the plan is in Admiry's review (L1 §6.3): where it stands; nothing to press (Task 7 words)
    n.classList.add("checks", "in-review");
    part(n, "launch-k").textContent = "In review";
    const st = part(n, "status");
    st.textContent = S.inReview || "";
    st.hidden = !S.inReview;
  }
  function changesLine(n) { // "Changes Admiry made" (spec 2026-10-11 §5.1): what Admiry removed after Google refused it, in review and once live
    const p = part(n, "changes");
    if (!p) return; // a card drawn without this part (a stub test tree): nothing to fill
    if (!S.changes.length) return p.remove();
    p.replaceChildren(el("strong", null, "Changes Admiry made. "), document.createTextNode(S.changes.join(" ")));
    p.hidden = false;
  }

  // ------------------------------------------------------------------ a signed-in owner's businesses
  // No visitor cookie (a sign-out deleted it, another browser, a business an assistant built) but a signed-in owner
  // with businesses here: ask first, and list them to pick from (founder's dev walk, 2026-10-07). A pick attaches
  // one (the cookies) and the load resumes it as a reload would; Start over shows the hero.
  const STATE_WORDS = { plan: "Plan ready", questions: "Questions left", none: "Nothing started yet" };
  function welcome(list) { // the log goes, and the setup card that lived in it (S.setup)
    Object.assign(S, { sessionId: null, status: null, cards: null, planNode: null, progress: null, question: null,
      setup: null, businessId: null });
    log.replaceChildren();
    moment(true);
    const one = list.length === 1 ? list[0] : null;
    admiry(!one ? `Welcome back. You have ${list.length} businesses here. Which one, or start over?`
      : one.state === "plan" ? `Welcome back. You already have a plan for ${one.name}. Continue reviewing it, or start over?`
      : one.state === "questions" ? `Welcome back. We started on ${one.name} and a few questions are left. Continue, or start over?`
      : `Welcome back. ${one.name} is here with nothing started yet. Continue with it, or start over?`);
    const n = clone("t-choose"), group = part(n, "options");
    for (const c of list) {
      const b = Object.assign(el("button", "opt"), { type: "button" });
      b.append(el("b", null, c.name), el("span", null, STATE_WORDS[c.state] || ""));
      b.addEventListener("click", () => pick(c));
      group.append(b);
    }
    part(n, "over").addEventListener("click", () => firstVisit());
    reveal(add(n), "start");
  }
  function pick(c) {
    work(async () => {
      const token = await csrf();
      const r = token ? await turn("pick", { business_id: c.id, csrf: token }) : { ok: false, error: "Sign in first." };
      if (!r.ok) return r.fresh ? firstVisit() : oops(r.error);
      log.querySelectorAll(".choose").forEach((x) => x.remove());
      you(c.name, true);
      visitor(r.visitor);
      await resume(await turn("state"));
    });
  }

  // ------------------------------------------------------------------ the wall and sign-in
  async function toWall(signin) { // signin: a claimed plan whose owner is not signed in here — Google only
    if (!signin && S.signedIn && !S.claimTried) return claimThenResume(); // the wall never shows to a signed-in owner
    log.querySelectorAll(".wall").forEach((w) => w.remove());
    const w = add(clone("t-wall")), h = w.querySelector("h2");
    if (signin) w.querySelectorAll(".ready, .muted, .divider, [data-sheet]").forEach((x) => x.remove());
    if (signin) h.textContent = "Sign in to continue";
    const way = new URLSearchParams(location.search).get("website"); // the plan e-mail's link survives the sign-in
    const to = `/?after=signin&website=${encodeURIComponent(way)}`; // (ruling R2-g)
    if (way) w.querySelector("a.btn").href = `/connect/google/start?next=${encodeURIComponent(to)}`;
    reveal(w, "center");
    h.focus({ preventScroll: true });
    announce(signin ? "Sign in to continue." : "Your plan is ready. Sign in to see it.");
  }
  async function claimThenResume() {
    S.claimTried = true;
    moment(true); // on a load with ?after=signin the conversation is still hidden: a refusal must be seen
    const token = await csrf();
    const c = token ? await post("/api/claim", { csrf: token }) : { ok: false, error: "Sign in first." };
    if (c.ok) return visitor(c.visitor), resume(await turn("state"), true);
    if (c.fresh) return firstVisit();
    oops(c.error);
    return toWall();
  }
  const recommend = async () => handle(await turn("recommend"), PLAN_INTRO);
  async function resume(r, afterSignIn) {
    if (r.signin) return moment(true), toWall(true);
    if (!r.ok && !r.fresh) return moment(true), oops(r.error); // a failed load: say so where the owner can answer
    const why = returning || r.reason === "moved" ? r.reason : null; // "expired" | "moved": say why it starts over
    if (!r.ok || !r.result || !r.result.session) return r.choose ? welcome(r.choose) : firstVisit(null, why);
    visitor(r.visitor);
    const st = r.result, s = st.session, fresh = !log.querySelector(".turn");
    S.status = s.status;
    moment(true);
    if (fresh && s.opening_text) you(s.opening_text);
    if (S.wall) return toWall();
    if (s.status === "planned" && st.cards) {
      const running = st.cards.channels && st.cards.channels.rows.length; // after launch: the channels card leads
      plan(
        st, // its cards, and the actions and launch_note the build answered (walk fixes 1): the same launch card
        st.next,
        !fresh ? PLAN_INTRO : running ? "Welcome back. Here is where your ads run." : "Welcome back. Here is your plan.",
      );
      return;
    }
    if (s.status === "ready") {
      if (afterSignIn) return recommend();
      admiry("Welcome back. I have what I need to build your plan.");
      return actions([BUILD]);
    }
    if (st.next && st.next.kind === "question") {
      if (fresh) admiry("Welcome back. Here is where we left off.");
      return question(st.next.question);
    }
    firstVisit();
  }

  // ------------------------------------------------------------------ the plan
  // the channels card's side of the page (static/channels.js draws it): the business for its links and, for its
  // Pause/Resume, the busy state, csrf and requests, the plan drawn again from `state`, and the page's messages
  const CARD = { business: () => S.businessId, work, csrf, post, turn, oops, announce, draw: (st) => plan(st, S.next) };
  function plan(d, next, intro) {
    // a tool's answer (recommend, adjust) carries the plan's cards only: the channels card shown stays as it was
    const c = d.cards.channels || !(S.cards && S.cards.channels) ? d.cards : { ...d.cards, channels: S.cards.channels };
    // the same version drawn again keeps its thumbs row (the vote, a half-filled form); a new one starts empty (spec §6)
    const kept = S.planNode && S.planId === d.plan_id ? S.planNode.querySelector(".rate") : null; // before S.planId changes
    const launchNote = "launch_note" in d ? d.launch_note : S.launchNote; // an answer's own; a redraw keeps the plan's
    Object.assign(S, { cards: c, planId: d.plan_id, status: "planned", wall: false, next, launchNote });
    S.planStatus = "plan_status" in d ? d.plan_status : "draft"; // a recommend or adjust answer is a new draft
    S.inReview = d.in_review || null; // a state read says the status
    S.changes = Array.isArray(d.changes) ? d.changes : []; // a state read lists what Admiry removed (spec 2026-10-11 §5.1)
    S.businessId = d.business_id || S.businessId;
    S.planUrl = d.plan_url || (/^pl_[0-9a-f]{16}$/.test(d.plan_id || "") ? `/plan/${d.plan_id}` : null);
    log.querySelectorAll(".actions, .wall").forEach((x) => x.remove());
    if (d.steps) progress(d.steps);
    const n = clone("t-plan");
    planCard(n, c.plan || {});
    budgetCard(n, c.budget || {}, (c.plan || {}).channels || []);
    economicsCard(n, c.economics || {});
    whyCard(n, c.rationale || {});
    adPreviewCard(n, c.ads_preview || {}, d.execution || null);
    pageCard(n, (c.plan && c.plan.setup && c.plan.setup.website) || null);
    launchCard(n, c, next, launchNote);
    changesLine(n);
    if (c.channels && c.channels.rows && c.channels.rows.length) n.prepend(window.AdmiryChannels.card(c.channels, CARD));
    if (kept) n.append(kept);
    else if (window.AdmirySupport) window.AdmirySupport.rate(n, { planId: d.plan_id, surface: "home", vote: d.feedback || null });
    if (intro) {
      if (S.planNode) S.planNode.remove();
      reveal(admiry(intro), "start");
      add(n);
    } else if (S.planNode && S.planNode.isConnected) S.planNode.replaceWith(n);
    else add(n);
    S.planNode = n;
  }
  function rows(dl, pairs) {
    for (const [k, v] of pairs) {
      if (v == null || v === "") continue;
      const row = el("div", "row");
      row.append(el("dt", null, k), el("dd", null, String(v)));
      dl.append(row);
    }
  }
  function results(list) {
    if (!Array.isArray(list) || !list.length) return null;
    const w = list.map((x) => RESULTS[x] || String(x).replace(/_/g, " "));
    return cap(w.length > 1 ? `${w.slice(0, -1).join(", ")}, or ${w[w.length - 1]}` : w[0]);
  }
  function planCard(n, p) { // one row per funded channel, then the later lines (spec 2026-09-29 §7.2; channels.js)
    const s = p.setup || {};
    part(n, "goal").textContent = p.goal || "";
    window.AdmiryChannels.planRows(part(n, "channels"), p);
    rows(part(n, "setup"), [["Area", cap(s.area)], ["Goal", cap(s.goal)], ["Offer", s.offer],
      ["Counts as a result", results(s.counts_as_result)], ["Runs", cap(s.runs)], ["Landing page", s.landing_page]]);
    websiteCard(n, s.website);
  }
  // The slider's stops, in whole dollars a day: every dollar where small budgets live, coarser above, and every
  // level's own amount, so a level is always a place the thumb can rest.
  function stops(lo, hi, extra) {
    const out = new Set(extra.filter((d) => d >= lo && d <= hi)).add(hi);
    for (let d = lo; d < hi; d += d < 50 ? 1 : d < 100 ? 5 : d < 300 ? 10 : 25) out.add(d);
    return [...out].sort((x, y) => x - y);
  }
  const daily = (monthly) => Math.floor(monthly / DAYS); // plans.cards per_day
  function budgetCard(n, b, rows) {
    const badge = part(n, "badge"), levels = Object.entries(b.levels || {});
    const lv = Object.fromEntries(levels.map(([k, v]) => [k, v.monthly_usd])); // the amounts; each level's split:
    const split = Object.fromEntries(levels.map(([k, v]) => [k, v.split]));
    const own = Object.fromEntries(rows.map((r) => [r.platform, r.monthly_usd])); // the plan's, at its own amount
    // a level clipped to the same daily amount as a cheaper one (an owner's limit) is not a separate choice
    const names = LEVELS.filter((k, i) => lv[k] && !LEVELS.slice(0, i).some((j) => lv[j] && daily(lv[j]) === daily(lv[k])));
    const paint = (monthly, level) => {
      part(n, "per-day").textContent = usd(daily(monthly));
      part(n, "monthly").textContent = `about ${usd(monthly)} a month`;
      part(n, "split").textContent = window.AdmiryChannels.split(level ? split[level] : monthly === b.monthly_usd ? own : null);
      badge.textContent = level ? cap(level) : "Custom";
      badge.classList.toggle("on", level === "recommended");
    };
    paint(b.monthly_usd || 0, LEVELS.includes(b.level) ? b.level : null);
    if (names.length < 2) return part(n, "slider").remove(), part(n, "segs").remove();
    const days = names.map((k) => daily(lv[k])), now = daily(b.monthly_usd || 0);
    const pts = stops(Math.min(...days), Math.max(...days, now), [...days, now]).map((day) => {
      const level = names.find((k) => daily(lv[k]) === day) || null;
      return { day, level, monthly: level ? lv[level] : day * DAYS };
    });
    const range = part(n, "range"), ticks = part(n, "ticks"), last = pts.length - 1;
    const pct = (i) => `${(i / last) * 100}%`;
    range.max = String(last);
    part(n, "low").textContent = `${usd(pts[0].day)}/day`;
    part(n, "high").textContent = `${usd(pts[last].day)}/day`;
    const marks = names.map((k) => {
      const i = pts.findIndex((pt) => pt.level === k), t = el("span", `tick ${k}`);
      t.style.setProperty("--at", pct(i));
      ticks.append(t);
      return [i, t];
    });
    const segs = [...part(n, "segs").children].filter((s) => (names.includes(s.dataset.level) || (s.remove(), false)));
    part(n, "segs").dataset.n = String(segs.length); // four wrap two by two on a phone
    const mark = (i) => {
      const pt = pts[i];
      range.value = String(i);
      range.setAttribute("aria-valuetext", `${usd(pt.day)} a day${pt.level ? `, ${pt.level}` : ""}`);
      range.style.setProperty("--fill", pct(i));
      marks.forEach(([j, t]) => t.classList.toggle("in", j <= i));
      segs.forEach((s) => s.setAttribute("aria-pressed", String(s.dataset.level === pt.level)));
      paint(pt.monthly, pt.level);
    };
    const economics = part(n, "economics").closest(".card");
    const commit = (i, selector) => {
      const pt = pts[i];
      setBudget(pt.level ? { level: pt.level } : { monthly_budget_usd: pt.monthly }, pt.monthly, selector);
    };
    const at = LEVELS.includes(b.level) ? pts.findIndex((pt) => pt.level === b.level) : pts.findIndex((pt) => pt.day === now);
    mark(Math.max(0, at));
    segs.forEach((s) => {
      const k = s.dataset.level;
      s.replaceChildren(el("span", "seg-name", cap(k)), el("span", "amt", `${usd(daily(lv[k]))}/day`));
      s.addEventListener("click", () => {
        const i = pts.findIndex((pt) => pt.level === k);
        mark(i);
        commit(i, `[data-level="${k}"]`);
      });
    });
    let timer = 0;
    range.addEventListener("input", () => (mark(Number(range.value)), economics.classList.add("stale")));
    range.addEventListener("change", () => {
      clearTimeout(timer); // arrow keys fire change per step; send only where the owner stops
      timer = setTimeout(() => commit(Number(range.value), '[data-part="range"]'), 350);
    });
  }
  function redraw(next) {
    plan({ cards: S.cards, plan_id: S.planId, plan_url: S.planUrl }, next === undefined ? S.next : next);
  }
  function setBudget(args, monthly, selector) {
    const cur = S.cards && S.cards.budget;
    if (!cur) return;
    // the same amount again (or a turn already under way): put the card back where the plan is
    if (monthly === cur.monthly_usd || S.busy) return redraw();
    work(async () => {
      const r = await turn("adjust", args);
      const ok = r.ok && r.result && r.result.data && r.result.data.cards;
      if (ok) visitor(r.visitor);
      // the new plan in place, or the old one again (the slider goes back to where the plan is)
      if (ok) plan(r.result.data, r.result.next);
      else redraw();
      const c = S.planNode.querySelector(selector);
      if (c) c.focus({ preventScroll: true });
      if (!ok) return handle(r);
      announce(`Budget set to ${usd(r.result.data.cards.budget.per_day)} a day. The estimates follow.`);
    });
  }
  function focusBudget() {
    const card = S.planNode && part(S.planNode, "budget");
    if (card) reveal(card, "center"), (part(card, "range") || card).focus({ preventScroll: true });
  }
  function economicsCard(n, e) {
    const cols = e.columns || {}, p = cols.planning, body = part(n, "economics");
    // Recommended beside the owner's own budget; one column when the owner's budget is the recommended one
    const c = cols.recommended && p && cols.recommended.spend !== p.spend ? cols.recommended : null;
    if (!c) part(n, "col-rec").remove();
    const tr = (label, a, b, cls) => {
      const row = el("tr", cls), th = el("th", null, label);
      th.scope = "row";
      row.append(th);
      if (c) row.append(el("td", null, a));
      row.append(el("td", null, b));
      body.append(row);
    };
    if (p) {
      const q = c || p; // the unused first cell when there is one column
      tr("Daily budget", `${usd(daily(q.spend))}`, `${usd(daily(p.spend))}`);
      tr("Monthly spend", usd(q.spend), usd(p.spend));
      tr("Estimated leads", span(q.leads), span(p.leads));
      tr("Estimated bookings", span(q.bookings), span(p.bookings));
      tr("Estimated new customers", span(q.customers), span(p.customers));
      const v = (e.assumptions || []).find((a) => a.name === "customer value");
      if (v) tr(v.basis === "your answer" ? "Customer value, your answer" : "Customer value, estimated", v.value, v.value);
      tr("Potential revenue", span(q.revenue, usdShort), span(p.revenue, usdShort), "total");
    }
    const ul = part(n, "assumptions");
    for (const a of e.assumptions || []) ul.append(el("li", null, `${cap(a.name)}: ${a.value} (${a.basis})`));
    part(n, "footnote").textContent = e.footnote || "";
  }
  function whyCard(n, r) {
    const ul = part(n, "searches");
    part(n, "why").textContent = r.why_primary || "";
    for (const q of r.example_searches || []) ul.append(el("li", "q", q));
    if (!ul.children.length) ul.remove();
    part(n, "why-not").textContent = r.why_not_later || "";
  }
  // "How your ad reads" (spec §7 ads_preview, A3 Task 5): the ad text exactly as it will run, and, once a plan
  // is approved and Admiry knows where the ads stand, the "Live since …" / "Launching" / "Paused" line.
  function adPreviewCard(n, p, execution) { // no headline left: still the picture approving approves, and its line
    const section = part(n, "ad-preview"), headlines = p.headlines || [];
    if (!headlines.length && !p.image_note) return section.remove();
    for (const [k, v] of [["ad-headlines", headlines.join(" · ")], ["ad-desc", (p.descriptions || []).join(" ")]])
      if (v) part(n, k).textContent = v; else part(n, k).remove();
    window.AdmiryChannels.picture(part(n, "ad-image"), p); // the picture a channel shows with the ad (channels.js)
    const host = part(n, "ad-host");
    if (p.final_host) host.textContent = `Sends visitors to ${p.final_host}`;
    else host.remove();
    const status = part(n, "ad-status");
    if (execution) status.textContent = execution;
    else status.remove();
  }
  function launchCard(n, c, next, note) {
    const p = c.economics && c.economics.columns && c.economics.columns.planning, setup = (c.plan || {}).setup || {};
    rows(part(n, "summary"), [["Runs on", ((c.plan || {}).channels || []).map((ch) => ch.label).join(", ")],
      ["Budget", c.budget ? `${usd(c.budget.per_day)} a day` : null], ["Area", cap(setup.area)],
      ["Estimated", p ? `${span(p.leads)} leads` : null], ["Landing page", (c.ads_preview || {}).final_host]]);
    // the server's actions only: none given, none drawn -- and the four ways once, on the website card (ruling R2-c)
    const ways = ((setup.website || {}).ways || []).map((w) => w.id);
    const list = (next && next.kind === "actions" ? next.actions : []).filter((a) => !ways.includes(a.id));
    // a launch button is its action's id (Admiry running it and the own account share connect_channel), a chip its tool
    const tools = new Map(list.map((a) => [a.tool, a])), ids = new Map(list.map((a) => [a.id, a]));
    n.querySelectorAll("[data-tool]").forEach((b) => {
      const a = b.dataset.id ? ids.get(b.dataset.id) : tools.get(b.dataset.tool);
      if (!a) return b.remove();
      if (a.label && !OWN_WORDS.has(a.id)) part(b, "label").textContent = a.label;
      if (b.classList.contains("two")) b.classList.toggle("dark", Boolean(a.primary));
      if (b.tagName === "BUTTON") b.addEventListener("click", () => act(a));
    });
    // the heading follows what is offered: "Sign in first" only beside a draft key's sign-in (SIGN_IN_ACTIONS), else
    // the two ways, "Next step:" for one, and for none the answer's launch_note on why nothing can launch yet
    const signIn = tools.has("request_connect_link"), kept = n.querySelectorAll(".launch-buttons [data-tool]");
    const offered = kept.length, heading = n.querySelector('[data-when="launch"]');
    if (offered === 1) kept[0].classList.toggle("dark", true); // the one way there is leads, prepaying alone too
    n.querySelector(`[data-when="${signIn ? "launch" : "sign-in"}"]`).remove();
    if (!signIn && offered === 1) heading.textContent = "Next step:";
    else if (!signIn && !offered && note) heading.textContent = note;
    else if (!signIn && !offered) heading.remove();
    n.querySelectorAll("[data-say]").forEach((b) => b.addEventListener("click", () => {
      reply.value = b.dataset.say; // "Change the area to …": the owner finishes the sentence, `say` reads it
      grow();
      reply.focus();
      reply.setSelectionRange(reply.value.length, reply.value.length);
    }));
    const a = part(n, "plan-link"), href = safeHref(S.planUrl);
    if (href) a.href = href;
    else a.parentElement.remove();
    // L1 (spec 2026-10-10 §4.3): a draft is checked before it is ready; a plan in review says where it stands
    if (S.planStatus === "in_review") return reviewing(n);
    if (S.planStatus === "draft" && S.claimed && !signIn) checkPlan(n);
  }

  // ------------------------------------------------------------------ your website (A3p-5 batch 2, Q5-Q8)
  // The plan's "Your website" row (site_install.row, in the plan's setup): its sentence, and for a site that needs
  // Admiry's code the ways it offers, in the server's order (the Admiry page first where a builder keeps code in a
  // frame; Admiry's team only while support is on). "I'll add it" opens the steps, the code and Check now; "Send it to
  // my web person" the address field (Q7); "Have Admiry's team do it" its contact field and button, which asks only on
  // the owner's tap (Q8, ruling R3-c); "Use an Admiry page instead" is the page card. No website of its own: no card.
  // The page's report (spec 2026-10-12 §3.5, R3), read once on draw: the page the ads use and its builder, the verdict,
  // the bar and the legend, then the checks by what they mean for the owner -- what stops the ads (the tracking row
  // with the ways as its own buttons while the code is missing, the ways row then not drawn), what is worth doing, the
  // passing ones folded -- and the foot: when it was checked, and Check again. No report yet: the ways, then "not
  // checked yet" and Check now.
  function websiteCard(n, w) {
    const card = part(n, "website");
    if (!w || !w.say) return card.remove();
    part(card, "website-say").textContent = w.say;
    siteLine(card, w);
    const ways = part(card, "website-ways");
    ways.hidden = true; // shown only once the load answers -- never drawn then hidden again on the same card (Task 8 fix)
    if (w.state === "needs_code") for (const way of w.ways || []) {
      const b = Object.assign(el("button", "btn", way.label), { type: "button" });
      b.dataset.way = way.id;
      b.addEventListener("click", () => chooseWay(n, way.id));
      ways.append(b);
    }
    const code = part(card, "website-code");
    part(card, "website-copy").addEventListener("click", () =>
      copyFrom(code.value, code, part(card, "website-copy-label"), "code"));
    for (const p of ["website-check", "website-first", "website-again"]) // Check now (the steps', "not checked yet"'s), again
      part(card, p).addEventListener("click", () => work(() => checkNow(n)));
    wayForm(card, "website-send", "website-to", "/api/page/install-send", "email", true);
    wayForm(card, "website-team", "website-reach", "/api/page/install-help", "contact", false);
    if (w.state !== "admiry_page") loadWebsite(n, w); // an Admiry page is the page card's to check
  }
  async function loadWebsite(n, w) { // the page's report, once, without waiting for it (not `work`ed, as loadPageState)
    const r = await post("/api/page/website", { csrf: await csrf() });
    // a Check now answered meanwhile is newer: this older report is not drawn over it
    if (!n.isConnected || part(n, "website").dataset.drawn) return;
    if (r.ok) showSiteCheck(n, r.check, r.website);
    else part(n, "website-ways").hidden = w.state !== "needs_code"; // refused: the card stands as the row drew it
  }
  function showSiteCheck(n, check, w) { // the list and the foot from a report; without one, the ways and "not checked yet"
    const card = part(n, "website"), state = w ? w.state : null, needs = state === "needs_code";
    card.dataset.drawn = "1";
    siteLine(card, w);
    drawChecks(card, check, needs ? { ways: w.ways, onCode: () => work(() => showSteps(card)),
      onSend: () => showWay(card, "website-send", "website-to"), onHelp: () => showWay(card, "website-team", "website-ask"),
      onPage: () => chooseWay(n, "admiry_page") } : null);
    part(card, "website-ways").hidden = Boolean(check) || !needs; // drawn, the ways are the tracking row's buttons
    part(card, "website-unchecked").hidden = Boolean(check) || !(needs || state === "found"); // found: no ways
    part(card, "website-foot").hidden = !check;
    if (check) part(card, "website-checked").textContent = `Checked ${when(check.checked_at)}`;
  }
  function siteLine(card, w) { // the page the ads use, and the builder it is on when Admiry can tell
    part(card, "website-host").textContent = w && w.url ? hostOf(w.url) : "";
    const tag = part(card, "website-platform");
    tag.textContent = (w && w.platform_name) || "";
    tag.hidden = !tag.textContent;
  }
  function hostOf(url) { // "marinabayroofing.com/lp/roof-repair": the host without "www.", and the path past "/"
    try {
      const u = new URL(/^https?:\/\//i.test(url) ? url : `https://${url}`);
      // a root path's own query (WordPress's plain permalinks, "/?page_id=42") is not the bare host
      return u.host.replace(/^www\./, "") + (u.pathname === "/" ? (u.search ? u.pathname + u.search : "") : u.pathname);
    } catch (e) { return String(url); }
  }
  function when(iso) { // a report's time in words; one without (Check now's own answer) was made just now
    const t = Date.parse(String(iso || "").replace(/(\.\d{3})\d+/, "$1")), s = Number.isNaN(t) ? 0 : (Date.now() - t) / 1000;
    const ago = (k, unit) => `${k} ${unit}${k === 1 ? "" : "s"} ago`;
    if (s < 60) return "just now";
    if (s < 3600) return ago(Math.floor(s / 60), "minute");
    return s < 86400 ? ago(Math.floor(s / 3600), "hour") : new Date(t).toLocaleDateString();
  }
  // A page's 14 checks (spec 2026-10-12 §3.5), into a host with the list's parts: the website card, the published
  // page's and the site's (SITE.checks). `opts`: the row's ways and what each does, for the tracking row while the
  // code is missing, or null.
  const VERDICTS = { ok: "Your ads can run here.", warned: "Your ads can run here. A few things are worth doing.",
    one: "One thing before the ads can run.", many: (n) => `${n} things before the ads can run.` };
  const BLOCKING = new Set(["reachable", "phone", "message_match", "tracking", "privacy", "compliance"]); // landing.BLOCKING
  function groupChecks(check) { // the rules by what they mean for the owner: what stops the ads, what is worth doing
    const rules = (check && check.rules) || [];
    const stop = rules.filter((r) => r.ok === false && BLOCKING.has(r.id));
    const warn = rules.filter((r) => r.ok === null || (r.ok === false && !BLOCKING.has(r.id)));
    const pass = rules.filter((r) => r.ok === true);
    const verdict = stop.length > 1 ? VERDICTS.many(stop.length) : stop.length === 1 ? VERDICTS.one
      : warn.length ? VERDICTS.warned : VERDICTS.ok;
    return { stop, warn, pass, verdict };
  }
  function checkRowOf(r, kind, fix) { // the mark, the title, the detail with its fix, the why; a passing row is one line
    const li = el("li", kind), mark = el("span", `mark ${kind}`, kind === "pass" ? "✓" : kind === "warn" ? "!" : "✕");
    const body = el("div"), t = el("p", "t", r.title), d = [r.detail, r.fix].filter(Boolean).join(" ");
    li.dataset.id = r.id;
    mark.setAttribute("aria-label", kind === "pass" ? "Passes" : kind === "warn" ? "Worth doing" : "Needs fixing");
    body.append(t);
    if (kind === "pass") { if (r.detail) t.append(el("span", "d", r.detail)); }
    else {
      if (d) body.append(el("p", "d", d));
      if (r.why) body.append(el("p", "why", r.why));
      if (fix) body.append(fix);
    }
    li.append(mark, body);
    return li;
  }
  function drawChecks(host, check, opts) { // the verdict, the bar, the legend and the three groups into `host`'s parts
    const g = groupChecks(check), head = part(host, "website-verdict"), groups = part(host, "website-groups");
    head.hidden = groups.hidden = !check;
    if (!check) return g;
    part(host, "website-verdict-line").textContent = g.verdict;
    const kinds = [["stop", g.stop, "before the ads can run"], ["warn", g.warn, "worth doing"], ["pass", g.pass, "pass"]]
      .filter(([, list]) => list.length), total = g.stop.length + g.warn.length + g.pass.length;
    part(host, "website-bar").replaceChildren(...kinds.map(([k, list]) => {
      const s = el("span", k);
      s.style.width = `${((100 * list.length) / total).toFixed(2)}%`;
      return s;
    }));
    part(host, "website-legend").replaceChildren(...kinds.map(([k, list, words]) => {
      const s = el("span");
      s.append(el("i", k), el("b", null, String(list.length)), doc.createTextNode(` ${words}`));
      return s;
    }));
    const group = (label, list, kind) => {
      const d = el("div", "check-group"), ul = el("ul", "checks");
      d.append(el("p", "k", label), ul);
      for (const r of list) ul.append(checkRowOf(r, kind, kind === "stop" ? fixRow(r, opts) : null));
      return d;
    };
    groups.replaceChildren();
    if (g.stop.length) groups.append(group("Before the ads can run", g.stop, "stop"));
    if (g.warn.length) groups.append(group("Worth doing", g.warn, "warn"));
    if (g.pass.length) { // folded: the work shows without crowding the card
      const d = el("details", "passing"), s = el("summary"), ul = el("ul", "checks"), mark = el("span", "mark pass", "✓");
      mark.setAttribute("aria-hidden", "true");
      s.append(mark, doc.createTextNode(g.pass.length === 1 ? " 1 check passes" : ` ${g.pass.length} checks pass`));
      for (const r of g.pass) ul.append(checkRowOf(r, "pass", null));
      d.append(s, ul);
      groups.append(d);
    }
    return g;
  }
  function fixRow(r, opts) { // the tracking row's buttons: the row's own ways, in its order; another row has its fix
    if (r.id !== "tracking" || !opts) return null;
    const words = { install_self: ["Add the code", opts.onCode], install_send: ["Send it to my web person", opts.onSend],
      install_help: ["Have Admiry's team do it", opts.onHelp], admiry_page: ["Use an Admiry page instead", opts.onPage] };
    const ways = (opts.ways || []).map((w) => words[w.id]).filter((way) => way && way[1]);
    if (!ways.length) return null;
    const row = el("div", "fix");
    ways.forEach(([label, onClick], i) => row.append(fixButton(label, i === 0, onClick)));
    return row;
  }
  function wayForm(card, name, field, path, key, needed) { // a way's field and button: sent on the owner's tap alone
    const form = part(card, name), input = part(form, field);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const value = input.value.trim();
      if (needed && !value) return; // the web person's address is needed; a contact for the team is not
      work(async () => {
        if (!(await wayAnswer(card, path, value ? { [key]: value } : {}))) return; // a refusal keeps it to fix
        form.hidden = true;
        input.value = "";
      });
    });
  }
  function chooseWay(n, id) {
    const card = part(n, "website");
    if (id === "install_self") return work(() => showSteps(card));
    if (id === "install_send") return showWay(card, "website-send", "website-to");
    if (id === "install_help") return showWay(card, "website-team", "website-ask"); // asked on its tap, never here
    if (id !== "admiry_page") return;
    const page = part(n, "page"), build = part(page, "page-build"), instead = part(page, "page-instead");
    instead.hidden = false; // "Your website stays as it is. Admiry builds a page for your ads." (ruling R2-f)
    announce(instead.textContent);
    if (page.hidden) { page.hidden = false; loadPageState(n); } // the look picker, hidden beside the website (R5)
    reveal(page, "start");
    if (!build.hidden) build.focus({ preventScroll: true });
  }
  async function showSteps(card) { // the steps for the page the ads use, its code to copy, and Check now
    const box = part(card, "website-steps");
    if (!box.dataset.loaded) {
      const r = await post("/api/page/website", { csrf: await csrf() });
      if (!r.ok) return handle(r);
      if (!r.install) return settle(card, r.website); // the row moved on meanwhile: nothing to add any more
      const i = r.install, one = i.steps.length === 1, help = part(box, "website-help"), href = safeHref(i.help_url);
      part(box, "website-how").textContent = i.how; // "Here is how to add it on Wix."
      Object.assign(part(box, "website-needs"), { textContent: i.requirement || "", hidden: !i.requirement });
      Object.assign(part(box, "website-one"), { textContent: one ? i.steps[0] : "", hidden: !one }); // said, not numbered
      part(box, "website-list").replaceChildren(...(one ? [] : i.steps.map((step) => el("li", null, step))));
      part(box, "website-code").value = i.snippet;
      help.hidden = !href;
      if (href) help.href = href;
      box.dataset.loaded = "1";
    }
    box.hidden = false;
    reveal(box);
  }
  function showWay(card, name, focus) { // a way's field and button, ready for the owner (an e-mail's link opens it too)
    const form = part(card, name);
    form.hidden = false;
    reveal(form);
    part(form, focus).focus({ preventScroll: true });
  }
  async function wayAnswer(card, path, extra) { // the web person's e-mail or Admiry's team: the answer, on the card
    const r = await post(path, { csrf: await csrf(), ...extra });
    if (r.wall || r.signin || r.fresh) return handle(r);
    said(card, r.ok ? r.say : sentence(r.error));
    return r.ok;
  }
  async function checkNow(n) { // the check, now, on the page the ads use; its answer, its list, and the row as it stands
    const r = await post("/api/page/check", { csrf: await csrf() }), card = part(n, "website");
    if (!r.ok) return handle(r);
    said(card, r.say);
    showSiteCheck(n, r.check, r.website);
    if (r.website && r.website.state !== "needs_code") settle(card, r.website);
  }
  function said(card, text) { // Check now's answer, or a way's: one line on the card, and read out
    const out = part(card, "website-answer");
    out.textContent = text;
    out.hidden = false;
    announce(text);
  }
  function settle(card, w) { // the row moved on (the code found, an Admiry page): its sentence; the ways and steps go
    const setup = S.cards && S.cards.plan && S.cards.plan.setup;
    if (setup) setup.website = w; // a redraw of the same version draws the row as it now stands
    if (w) part(card, "website-say").textContent = w.say;
    for (const p of ["website-ways", "website-steps", "website-send", "website-team"]) part(card, p).hidden = true;
  }

  // ------------------------------------------------------------------ the page (Admiry Pages, Task 7, H14)
  // the industry site's side of the page (static/sites.js draws the card from the site's state, A3p-6 batch 2 Q17)
  const SITE = { post, csrf, work, handle, announce, safeHref, el, clone, part, sentence, reveal,
    reload: () => location.reload(), published: () => recheck(), checks: (host, check) => drawChecks(host, check, null) };
  function pageCard(n, website) { // hidden beside a website of the business's own until "Use an Admiry page instead" (spec 2026-10-12 §3.7)
    const card = part(n, "page");
    part(n, "page-build").addEventListener("click", () => work(() => buildPage(n)));
    if (website && website.state !== "admiry_page") return void (card.hidden = true);
    loadPageState(n);
  }
  async function loadPageState(n) {
    const r = await post("/api/page/state", { csrf: await csrf() });
    if (!n.isConnected || !r.ok || !r.page) return; // no page yet (or this card is gone): the button stays
    showPage(n, r.page);
  }
  async function buildPage(n) {
    const r = await post("/api/page/create", { csrf: await csrf() });
    if (!r.ok) return handle(r);
    showPage(n, r);
    if (r.kind !== "site") announce("Your page is ready to look over."); // a site's card starts with its looks
  }
  function showPage(n, p) {
    part(n, "page-intro").hidden = true;
    part(n, "page-build").hidden = true;
    const host = part(n, "page-body");
    host.hidden = false;
    drawPage(host, p);
  }
  function drawPage(host, p) { // the page's own card in `host`: Admiry's site, the assistant's live page, or the preview
    if (p.kind === "site") return void window.AdmirySites.card(host, p, SITE); // Admiry's own site (sites.js)
    // the assistant's own page is live (A3p-3 P23): it is theirs now, so no preview and no quick changes here
    const managed = p.managed_by === "assistant";
    host.replaceChildren(clone(managed ? "t-page-managed" : "t-page"));
    (managed ? fillManaged : fillPage)(host.firstElementChild, p);
  }
  async function buildHere() { // "Build your business website" with no plan on the page (follow-up R5): the page's
    // card in Admiry's own message -- the card the plan's website card would hold
    const r = await post("/api/page/create", { csrf: await csrf() });
    if (!r.ok) return handle(r);
    const n = admiry(r.kind === "site" ? "Pick a look for your website." : "Your page is ready to look over.");
    const host = el("div", "page-here");
    part(n, "body").append(host);
    drawPage(host, r);
    reveal(n);
  }
  function fillManaged(card, p) {
    part(card, "managed").textContent = p.say || "";
    const shot = part(card, "shot"); // A3p-3 batch 4: the render check's screenshot of the assistant's live site
    if (p.shot_url) { shot.src = p.shot_url; shot.hidden = false; }
    setPublished(card, p.status === "published" ? { url: p.url } : null, p.check);
    setHosting(card, p);
  }
  function fillPage(card, p) {
    card.dataset.token = p.token;
    part(card, "frame").src = p.preview_url;
    const photos = part(card, "photos");
    photos.hidden = !p.upload_url;
    if (p.upload_url) photos.href = p.upload_url;
    setPublished(card, p.status === "published" ? { url: p.url } : null, p.check);
    setHosting(card, p);
    part(card, "publish").addEventListener("click", () => work(() => publishPage(card)));
    part(card, "change").addEventListener("submit", (e) => {
      e.preventDefault();
      const input = part(card, "change-input"), text = input.value.trim();
      if (text) work(() => revisePage(card, text));
    });
  }
  // the hosting minimum (A3p-3 P15): the countdown's or the takedown's sentence when there is one, else the note the
  // owner reads before publishing; the Muse invitation once the page is live (P22); the download (P19)
  function setHosting(card, p) {
    const note = part(card, "hosting"), text = (p.hosting && p.hosting.say) || p.hosting_note || "";
    note.textContent = text;
    note.hidden = !text;
    const muse = part(card, "muse");
    if (muse) setMuse(card, p.muse);
    const download = part(card, "download"), href = safeHref(p.download_url);
    download.hidden = !href;
    if (href) download.href = href;
  }
  function setMuse(card, text) {
    const muse = part(card, "muse");
    part(muse, "muse-text").textContent = text || "";
    muse.hidden = !text;
  }
  function setPublished(card, page, check) {
    const status = part(card, "status");
    if (part(card, "website-groups")) drawChecks(card, page && check, null); // the starter page's 14 checks under it
    if (!page) return void (status.hidden = true);
    const rules = (check && check.rules) || [];
    const passed = rules.filter((r) => r.ok === true).length;
    status.textContent = `Published at ${page.url}` + (rules.length ? ` (${passed} of ${rules.length} checks pass)` : "");
    status.hidden = false;
  }
  async function publishPage(card) {
    const r = await post("/api/page/publish", { csrf: await csrf(), token: card.dataset.token });
    if (!r.ok) return handle(r);
    setPublished(card, r.page, r.check);
    setHosting(card, r);
    announce(`Published at ${r.page.url}.`);
    await recheck();
  }
  async function revisePage(card, text) {
    const r = await post("/api/page/revise", { csrf: await csrf(), instruction: text });
    if (!r.ok) return handle(r);
    if (r.beyond_template) { // more than the template holds (A3p-3 P22): nothing changed; the assistant can do it
      setMuse(card, r.say);
      return announce(r.say);
    }
    card.dataset.token = r.token;
    part(card, "frame").src = r.preview_url;
    const photos = part(card, "photos");
    photos.hidden = !r.upload_url;
    if (r.upload_url) photos.href = r.upload_url;
    part(card, "change-input").value = "";
    part(card, "status").hidden = true; // the iframe now shows a fresh, unpublished draft; Publish it again to say so
    drawChecks(card, null, null); // and its checks with it
    setHosting(card, r);
    announce("Page updated.");
  }

  // ------------------------------------------------------------------ the assistant sheet
  const handoffLive = () => Boolean(S.handoff && S.handoff.until > Date.now());
  const promptText = () => (handoffLive() ? S.handoff.prompts : PROMPTS)[S.tab] || "";
  function sheet() {
    if (S.sheet) return S.sheet;
    const d = (S.sheet = clone("t-sheet")), tabs = [...d.querySelectorAll('[role="tab"]')];
    doc.body.append(d);
    tabs.forEach((t, i) => {
      t.addEventListener("click", () => tab(t.dataset.tab));
      t.addEventListener("keydown", (e) => {
        const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
        if (!step) return;
        e.preventDefault();
        const to = tabs[(i + step + tabs.length) % tabs.length];
        tab(to.dataset.tab);
        to.focus();
      });
    });
    part(d, "close").addEventListener("click", () => d.close());
    d.addEventListener("click", (e) => e.target === d && d.close()); // a click on the backdrop
    const share = part(d, "share");
    if (navigator.share) share.addEventListener("click", () => navigator.share({ text: promptText() }).catch(() => {}));
    else share.remove();
    part(d, "copy").addEventListener("click", copy);
    const open = part(d, "open"); // copies first; no URL scheme, no universal link (runbook B0): a phone gets the store
    open.addEventListener("click", () => void copy());
    open.href = MUSE_STORE[/iPhone|iPad|iPod/.test(navigator.userAgent) ? "ios" : /Android/.test(navigator.userAgent) ? "android" : "web"];
    return d;
  }
  function tab(name) {
    const d = S.sheet;
    S.tab = name;
    d.querySelectorAll('[role="tab"]').forEach((t) => {
      t.setAttribute("aria-selected", String(t.dataset.tab === name));
      t.tabIndex = t.dataset.tab === name ? 0 : -1;
    });
    d.querySelector('[role="tabpanel"]').setAttribute("aria-labelledby", `tab-${name}`);
    part(d, "title").textContent = `Continue in ${ASSISTANTS[name]}`;
    d.querySelectorAll(".sheet-panel [data-tab]").forEach((x) => (x.hidden = x.dataset.tab !== name));
    fillPrompt();
  }
  function fillPrompt() {
    const d = S.sheet, text = promptText(), cli = text.match(/claude mcp add .*?\/mcp(?=[.\s]|$)/);
    part(d, "prompt").value = text;
    part(d, "cli").hidden = !(S.tab === "claude" && cli);
    part(d, "cli-line").textContent = cli ? cli[0] : "";
    part(d, "foot").textContent = "You never copy a key." +
      (handoffLive() ? " The session code expires in 30 minutes." : "") +
      (navigator.share && S.tab === "muse" ? " On a phone, Share sends this straight to Muse." : "");
  }
  async function openSheet(name) {
    const d = sheet();
    tab(name);
    if (!d.open) d.showModal();
    if (!S.sessionId || !["open", "ready", "planned"].includes(S.status) || handoffLive()) return;
    const copyButton = part(d, "copy");
    copyButton.disabled = true; // the prompt is about to gain the session code
    try {
      const token = S.signedIn ? await csrf() : null;
      const r = await post("/api/handoff", token ? { csrf: token } : {});
      if (r.ok && r.token && r.prompts) {
        S.handoff = { prompts: r.prompts, until: Date.now() + Math.max(60, (r.expires_in || 1800) - 60) * 1000 };
      }
      part(d, "note").hidden = Boolean(r.ok && r.token); // 429/403: say the prompt starts afresh
    } finally {
      copyButton.disabled = false;
      fillPrompt();
    }
  }
  function copy() {
    return copyFrom(promptText(), part(S.sheet, "prompt"), part(S.sheet, "copy-label"), "message");
  }
  async function copyFrom(text, area, label, what) { // the sheet's message, the website card's code
    let ok = false;
    try { await navigator.clipboard.writeText(text), (ok = true); } catch (e) {
      area.focus();
      area.select(); // no async clipboard (plain http, older browsers): the old way, and the text stays selected
      try { ok = doc.execCommand("copy"); } catch (e2) { ok = false; }
    }
    label.textContent = ok ? "Copied" : "Selected, copy it";
    announce(ok ? "Copied." : `The ${what} is selected; copy it with your keyboard.`);
    setTimeout(() => (label.textContent = "Copy"), 2400);
  }

  // ------------------------------------------------------------------ input
  const grow = () => ((reply.style.height = "auto"), (reply.style.height = `${Math.min(reply.scrollHeight, 160)}px`),
    (reply.style.overflowY = reply.scrollHeight > 160 ? "auto" : "hidden")); // a scrollbar only past the cap
  function enterSends(area, fn) {
    area.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) e.preventDefault(), fn();
    });
  }
  function begin(text) {
    work(async () => {
      moment(true);
      you(text);
      const r = await turn("start", { text });
      if (r.ok) S.claimTried = false;
      else if (r.signin) return admiry(r.error), toWall(true); // "continue my plan" from nobody: sign in first
      else (reply.value = text), grow(); // one tap to try again
      await handle(r);
    });
  }
  function send() {
    const text = reply.value.trim();
    if (!text || S.busy) return;
    reply.value = "";
    grow();
    if (!S.sessionId) return begin(text);
    work(async () => {
      const q = S.status === "open" && S.question && S.question.isConnected ? S.question : null;
      if (q) spend(q, true); // free text answers the open question
      you(text);
      const r = await turn("say", q ? { text, question_id: q.dataset.qid } : { text });
      if (r.ok && r.start_over) return firstVisit(null, "over"); // the words asked to start over (harness, K6 slice 1)
      if (!r.ok && q) spend(q, false);
      if (!r.ok && r.fresh) return firstVisit(text);
      if (!r.ok) (reply.value = text), grow(); // one tap to try again
      await handle(r, "Done. Here is the plan with that change.");
    });
  }
  function startFromHero() {
    const text = ask.value.trim();
    if (S.busy) return;
    if (!text) return ask.focus();
    ask.value = "";
    begin(text);
  }
  $("ask-form").addEventListener("submit", (e) => (e.preventDefault(), startFromHero()));
  enterSends(ask, startFromHero);
  $("composer").addEventListener("submit", (e) => (e.preventDefault(), send()));
  enterSends(reply, send);
  reply.addEventListener("input", grow), window.addEventListener("resize", grow); // typing, and a rotation
  doc.querySelectorAll("[data-fill]").forEach((b) => b.addEventListener("click", () => {
    ask.value = b.dataset.fill || b.textContent.trim(); // a chip names an industry and fills its whole sentence
    ask.focus();
    ask.setSelectionRange(ask.value.length, ask.value.length);
  }));
  // the hero box types its examples in turn (the website's, hero-composer.tsx): hold, erase, type the next. Never
  // under reduced motion, nor while the visitor is in the box or has written in it: then it holds the whole sentence
  const examples = (ask.dataset.examples || "").split("|"); // the placeholder already holds the first, whole
  let ex = 0, shown = examples[0].length, phase = "hold";
  if (!reduced && examples.length > 1) setTimeout(typing, 3200);
  function typing() {
    let wait = 3200;
    if (ask.matches(":focus") || ask.value || hero.hidden) (shown = examples[ex].length), (phase = "hold");
    else if (phase === "hold") (phase = "erase"), (wait = 20);
    else if (phase === "erase") {
      shown = Math.max(0, shown - 4);
      wait = shown ? 16 : 380;
      if (!shown) (ex = (ex + 1) % examples.length), (phase = "type");
    } else (shown += 1), (wait = shown < examples[ex].length ? 34 : 3200), (phase = wait === 3200 ? "hold" : "type");
    ask.placeholder = examples[ex].slice(0, shown);
    setTimeout(typing, wait);
  }
  doc.addEventListener("click", (e) => {
    const opener = e.target.closest("[data-sheet]");
    if (opener) openSheet(opener.dataset.sheet); // (the header's "soon" links are site.js's, as on Settings)
  });

  // ------------------------------------------------------------------ load
  async function boot() {
    const back = new URLSearchParams(location.search), afterSignIn = back.get("after") === "signin";
    back.delete("after"); // back from the wall's sign-in: the plan e-mail's way stays until its card is found
    if (afterSignIn) history.replaceState(null, "", location.pathname + (back.toString() ? `?${back}` : ""));
    try {
      await work(async () => {
        const r = await turn("state");
        doc.body.classList.remove("returning");
        if (afterSignIn && r.ok && r.visitor && r.visitor.signed_in && !r.visitor.claimed) {
          return visitor(r.visitor), claimThenResume();
        }
        return resume(r, afterSignIn);
      });
    } finally {
      doc.body.classList.remove("returning");
    }
  }
  // A setup link's conversation under its card: a business with no consultation -- a plan an assistant made -- has none
  // to draw, so the card stays in view instead of the first visit's start page (follow-up R5), without the composer for
  // good: typed words would start a consultation; the question's own field takes the answer (follow-up final review C1)
  async function underCard(r) {
    if (r && r.ok && r.result && !r.result.session) return moment(true), (composer.hidden = true);
    return resume(r);
  }
  // /?setup=<business> (assistants, the receipt e-mail, the plan page and Stripe's return link here): the page becomes
  // that business's before its card opens -- `pick` attaches it (owner-checked) when the visitor cookie is another's or
  // none, and its conversation is drawn -- so what the card hands the conversation is fixed for the card's business.
  // Back from Stripe (`paid`) the card is alone until it says the money is in (`onSettled`), so the plan's launch card
  // never offers a second payment beside "being confirmed" (follow-up R4). A business not this owner's: the card alone
  // says so, and its going boots the home as it stands (final review C1).
  async function setupLink(id, paid) {
    let ours = false, r = null;
    try {
      await work(async () => {
        r = await turn("state");
        if (!(r.ok && r.result && r.result.business_id === id)) {
          const token = await csrf();
          // a new business's questions under way in this browser (an unclaimed draft): kept among the owner's
          // businesses by claiming it before the page switches (follow-up R2); not kept -- nothing switches
          if (token && r.ok && r.result && r.visitor && r.visitor.claimed === false) {
            const claimed = await post("/api/claim", { csrf: token });
            if (!claimed.ok) return;
          }
          const picked = token ? await turn("pick", { business_id: id, csrf: token }) : { ok: false };
          if (!picked.ok) return;
          visitor(picked.visitor);
          r = paid ? null : await turn("state");
        } else visitor(r.visitor); // already the link's business: its own pages name it too (follow-up final review I1)
        ours = true;
        if (!paid) await underCard(r);
      });
    } finally {
      // the card alone (paid, or not switched): no typed words start a consultation or draw a launch card beside it --
      // the composer comes back with the conversation (moment); the dock's other ways stay (follow-up final review C1)
      if (!ours || paid) composer.hidden = true;
      if (!ours) openSetup(null, id, paid, boot);
      else if (!paid) openSetup(null, id, false, null); // the conversation drawn once: the card going leaves it as it is
      else {
        let drawn = false, host = null; // once: when the money is in, or when the card goes before it is
        const draw = async () => {
          if (drawn) return;
          drawn = true;
          while (S.busy) await new Promise((ok) => setTimeout(ok, 100)); // after a turn of the owner's on its way
          // the conversation, then the card below it and in view (follow-up final review M1) -- but another business's,
          // when another tab switched this browser meanwhile, is not drawn here: the reload is offered (M2)
          await work(async () => {
            const st = await turn("state");
            if (st.ok && st.result && st.result.business_id !== id) return reloadOffer(OTHER_PAGE);
            await underCard(st);
            if (host.isConnected) log.append(host), reveal(host, "start");
          });
        };
        host = openSetup(null, id, true, draw, draw);
      }
    }
  }
  const q = new URLSearchParams(location.search), setupId = q.get("setup"), wanted = q.get("website");
  // The plan e-mail's four ways link here (/?website=<way id>, site_install.email_section): once the plan is drawn,
  // that way opens on the "Your website" card. The address keeps it until the card is found -- through a sign-in too
  // (ruling R2-g) -- then it is gone.
  function openWay(id) {
    const card = S.planNode && part(S.planNode, "website");
    if (!card) return; // no card on the page yet (the sign-in wall, a fresh start): the address keeps the way
    history.replaceState(null, "", location.pathname);
    const b = [...card.querySelectorAll("[data-way]")].find((x) => x.dataset.way === id);
    if (!b) return;
    reveal(card, "start");
    b.focus({ preventScroll: true });
    chooseWay(S.planNode, id);
  }
  // The website's front page (admiry.ai/ for a visitor without Admiry cookies) hands its hero sentence over as
  // /?start, the text in sessionStorage (same origin, never in the URL, so never in a log). Read once, then gone.
  function handedOver() {
    if (!q.has("start")) return "";
    history.replaceState(null, "", location.pathname);
    let text = "";
    try {
      text = sessionStorage.getItem(HANDOFF_KEY) || "";
      sessionStorage.removeItem(HANDOFF_KEY);
    } catch (e) { /* storage blocked: the visitor types it again */ }
    return text.trim().slice(0, 600);
  }
  // The daily digest's "Open Admiry" (/?report=<business>&day=<D>): behind sign-in; the conversation loads as always,
  // then that morning's report follows as one turn of Admiry's -- text only (textContent), never markup.
  async function showReport(id, day) {
    const p = new URLSearchParams({ b: id });
    if (day) p.set("day", day);
    try {
      const r = await (await fetch(`/api/report?${p}`, { credentials: "same-origin" })).json();
      if (!r.ok || !r.report) return;
      const rep = r.report, body = part(admiry(rep.sentence || ""), "body");
      for (const row of rep.rows || []) {
        const n = (k, w) => `${row[k]} ${w}${row[k] === 1 ? "" : "s"}`;
        const line = row.channel === "page" ? `Your page: ${n("leads", "lead")}`
          : `${row.label}: ${usd(row.spend_usd)} spent, ${n("calls", "call")}, ${n("forms", "form")}, ${n("leads", "lead")}`;
        body.append(el("p", "msg", line));
      }
      for (const it of rep.items || []) body.append(el("p", "msg", it.text));
      if (rep.note) body.append(el("p", "msg", `Admiry's take: ${rep.note}`));
      for (const s of rep.recommendations || []) body.append(el("p", "msg", s.text));
    } catch (e) { /* no report to show: the conversation stands */ }
  }
  const reportId = q.get("report");
  const toSignIn = () => location.assign(`/connect/google/start?next=${encodeURIComponent(location.pathname + location.search)}`);
  if (reportId && !S.signedIn) toSignIn();
  else if (reportId) {
    const day = q.get("day");
    history.replaceState(null, "", location.pathname);
    boot().finally(() => showReport(reportId, day));
  } else if (wanted && !setupId && !S.signedIn) toSignIn(); // the plan e-mail's card is behind sign-in (R2-g)
  else if (!setupId) {
    const handed = handedOver();
    boot().finally(() => {
      if (wanted) openWay(wanted);
      if (!handed || S.busy) return;
      if (S.sessionId) (reply.value = handed), grow(), reply.focus(); // a conversation exists: the owner sends it
      else (ask.value = handed), startFromHero();
    });
  } else if (!S.signedIn) toSignIn();
  else {
    history.replaceState(null, "", location.pathname);
    doc.body.classList.remove("returning");
    moment(true);
    setupLink(setupId, q.get("paid") === "1");
  }
})();
