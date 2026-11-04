/* The ads setup cards (spec 2026-09-29 setup-in-conversation): how the ads run, the documents to accept, then the
   owner's own Google Ads account or a payment. Every card follows POST /api/setup's `state` (the server decides the
   step); home.js hands this a container in the conversation. Dynamic strings go in with textContent. */
"use strict";
window.AdmirySetup = (() => {
  const doc = document;
  const el = (tag, cls, text) =>
    Object.assign(doc.createElement(tag), cls ? { className: cls } : {}, text != null ? { textContent: text } : {});
  const button = (cls, text, onClick) => {
    const b = Object.assign(el("button", cls, text), { type: "button" });
    if (onClick) b.addEventListener("click", onClick);
    return b;
  };
  const usd = (n) => "$" + Math.round(Number(n)).toLocaleString("en-US"); // whole dollars: the pay form's limits
  // balances keep their cents when they have any: $930, $0.40, $1,234.50
  const money = (n) => {
    const v = Number(n) || 0, whole = Number.isInteger(v);
    return "$" + v.toLocaleString("en-US", { minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: 2 });
  };
  // a dollar amount in cents, always with its cents: $1,033.30 (the pay form's prices)
  const cents = (c) => "$" + (c / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  // wallet.quote's arithmetic, to the cent (test_setup's parity test): what a way of paying costs for a credit
  const quoteCents = (credit, rail, r) =>
    rail === "ach"
      ? Math.min(Math.floor((credit * r.ach_bps + 5000) / 10000), r.ach_cap_cents)
      : Math.floor((credit * r.card_bps + 5000) / 10000) + r.card_fixed_cents;
  // Admiry running the ads first (founder's ruling, 2026-10-07), as the plan's launch card offers it
  const MODES = [
    { id: "managed", label: "Admiry runs the ads, I prepay", sub: "No Google account needed. Spent on the ads at the platform's exact cost." },
    { id: "own_account", label: "I have a Google Ads account", sub: "Your own account; Google bills you directly." },
  ];
  // the ways the server offers for the latest plan (`state.ways`, walk fixes 1): none named, every way, as before;
  // a way the business already took stays in its Change view, so the view always has a way back
  const offered = (st) =>
    MODES.filter((m) => !st.ways || st.ways.includes(m.id) || (st.step !== "choose" && m.id === st.mode));
  const LINK_WORDS = {
    refused: "The invitation was declined in Google Ads. Send it again when you are ready.",
    cancelled: "The invitation was withdrawn or has expired.",
    inactive: "That Google Ads account is no longer linked to Admiry. Send the invitation again to link it.",
  };
  // back from Stripe, the note once twenty looks end with the payment still not in (follow-up final review I2)
  const NOT_YET = "Your payment has not shown up yet. Reload this page in a few minutes before you pay again.";

  async function call(action, extra) {
    let j = null;
    try {
      const r = await fetch("/api/setup", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, ...extra }),
      });
      j = await r.json();
    } catch (e) {
      return { ok: false, error: "I could not reach Admiry. Check your connection and try again." };
    }
    if (!j || typeof j !== "object") return { ok: false, error: "Something went wrong on our side. Try again soon." };
    if (!("ok" in j)) {
      const said = [j.detail, j.error].find((m) => typeof m === "string" && m);
      return { ok: false, error: said || "Something went wrong. Reload and try again." };
    }
    return j;
  }

  // The full text in a sheet, without leaving the conversation: our own /legal page, parsed (scripts never run in a
  // parsed document) and its article moved in; the page's version line and back link stay out.
  // The title and a close icon stay on top while the text scrolls (sticky, home.css), and Done ends it: on a phone the
  // sheet is longer than the screen and there is no Escape key.
  async function openLegal(d) {
    const sheet = el("dialog", "sheet legal"), head = el("div", "legal-head"), body = el("div", "legal-body");
    const close = button("close", "✕", () => sheet.close());
    close.setAttribute("aria-label", "Close");
    const title = Object.assign(el("p", "k", `${d.title} · ${d.version}`), { id: `legal-${d.id}` });
    sheet.setAttribute("aria-labelledby", title.id);
    head.append(title, close);
    sheet.append(head, body, button("btn dark legal-done", "Done", () => sheet.close()));
    sheet.addEventListener("close", () => sheet.remove());
    doc.body.append(sheet);
    sheet.showModal();
    try {
      const res = await fetch(d.url, { credentials: "same-origin" });
      if (!res.ok) throw new Error("status " + res.status);
      // the page's own <style> would be blocked by the home's CSP (and is not wanted here): drop it before parsing
      const text = (await res.text()).replace(/<style[\s\S]*?<\/style>/gi, "");
      const page = new DOMParser().parseFromString(text, "text/html");
      page.querySelectorAll("p.muted").forEach((p) => p.remove());
      body.append(...[...page.body.children].map((n) => doc.importNode(n, true)));
    } catch (e) {
      body.append(el("p", null, "Could not load the text. "), Object.assign(el("a", null, "Open it"), { href: d.url, target: "_blank" }));
    }
  }

  function run(o) {
    // looks: the refreshes asked so far; again: one asked while a call was on its way, run when that call ends
    const S = { state: null, busy: false, changing: false, more: false, waiting: false, looks: 0, again: false, gone: false };
    // the business named: a setup link's, or the one the page under a plan shows (follow-up R1: a tab left open while
    // another switched the business still sets up the one it shows); none -- the visitor cookie's (a draft's card)
    const where = o.businessId ? { business_id: o.businessId } : o.page ? { business_id: o.page } : {};
    const card = el("section", "card setup"), err = el("p", "setup-error"), live = el("p", "note setup-note-live");
    err.setAttribute("role", "alert");
    live.setAttribute("role", "status");
    live.textContent = "Your payment is being confirmed; this updates when it lands.";
    o.host.append(card);

    async function go(action, extra = {}) {
      if (S.busy) return null;
      S.busy = true;
      card.classList.add("busy");
      err.textContent = "";
      const token = action === "state" ? undefined : await o.csrf();
      const r = await call(action, { ...where, ...extra, csrf: token });
      S.busy = false;
      card.classList.remove("busy");
      // opened from Launch (no business in the address): say so on the card; a /?setup= link is home.js's to handle
      if (r.signin && !o.businessId) return card.replaceChildren(el("p", null, "Sign in first.")), null;
      if (r.gone || r.signin) return (S.gone = true), o.onGone && o.onGone(), null;
      const again = S.again; // a refresh that came meanwhile: run once this answer is read (final review I5)
      S.again = false;
      if (!r.ok) {
        if (again) await look(); // the card as it now stands, then this call's refusal on it
        return (err.textContent = r.error), card.append(err), null;
      }
      if (r.url) return location.assign(r.url), r;
      S.state = r.state;
      S.changing = false;
      if (r.launch) S.launch = r.launch;
      if (again) return look();
      paint();
      return r;
    }
    function look() { // where the setup stands now
      return go("state");
    }
    function paint() {
      const st = S.state;
      card.replaceChildren(el("p", "k", `Set up ads for ${st.name}`), ...how(st), ...step(st));
      if (err.textContent) card.append(err);
      if (S.waiting) card.append(live);
    }
    function how(st) {
      if (st.step === "claim") return [el("p", null, "Claim this business first: sign in with the link your assistant gives you.")];
      if (st.step !== "choose" && !S.changing) {
        const row = el("p", "setup-how", MODES.find((m) => m.id === st.mode).label + " · ");
        row.append(button("linkish", "Change", () => ((S.changing = true), paint())));
        return [row];
      }
      const group = el("div", "options cards");
      for (const m of offered(st)) {
        const b = button("opt" + (S.changing && m.id === st.mode ? " on" : ""), null, () => go("choose", { mode: m.id }));
        b.append(el("b", null, m.label), el("span", null, m.sub));
        b.disabled = Boolean(S.changing && st.locked && m.id !== st.mode);
        group.append(b);
      }
      const out = [group];
      if (st.ways_note) out.push(el("p", "note", st.ways_note)); // why a way is left out: the server's sentence
      if (S.changing && st.locked) out.push(el("p", "note", st.locked));
      return out;
    }
    function step(st) {
      if (S.changing || st.step === "choose" || st.step === "claim") return [];
      if (st.step === "accept") return [...channels(st), ...accept(st)];
      if (st.step === "link") return [...channels(st), ...linkForm(st)];
      // no pay form while a payment is being confirmed: never a second one beside it (follow-up final review I2)
      if (st.step === "pay" || (st.step === "done" && S.more)) return [...channels(st), ...(S.waiting ? [] : payForm(st))];
      return [...channels(st), ...done(st)];
    }
    // "Ready to launch" (spec 2026-09-29 §4 item 2): one step per channel the plan funds, each ticked once its account
    // is ready -- Google Search's Customer ID, NewsBreak's "nothing to connect" with what it waits for and its page's
    // fix (M1 follow-up 2, W3, W4) and its line about the plan's picture (M1 Task 7) -- above the step at hand
    function channels(st) {
      if (!st.steps || !st.steps.length) return [];
      const list = el("ul", "setup-steps");
      for (const s of st.steps) {
        const li = el("li", s.done ? "done" : "todo");
        li.append(el("b", null, (s.done ? "✓ " : "") + s.label), el("span", null, s.text));
        if (s.reason) li.append(el("span", "note", s.reason));
        if (s.page) li.append(el("span", "note", s.page));
        if (s.image) li.append(el("span", "note", s.image));
        list.append(li);
      }
      return [list];
    }
    function accept(st) {
      const todo = st.documents.filter((d) => !d.accepted), boxes = [];
      const list = el("div", "setup-docs");
      for (const d of todo) {
        const label = el("label", "setup-doc"), box = Object.assign(el("input"), { type: "checkbox" });
        // a link, not a button: it wraps inside the sentence on a phone (a button is one unbreakable box), and
        // without the sheet it still opens the text
        const name = Object.assign(el("a", null, d.title), { href: d.url });
        name.addEventListener("click", (e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey) return; // a new tab or window, as any link
          e.preventDefault();
          openLegal(d);
        });
        const words = el("span", "setup-doc-text", "I accept the "); // one block beside the box: it wraps as a sentence
        words.append(name, el("span", "muted", ` (${d.version})`));
        label.append(box, words);
        boxes.push(box);
        list.append(label);
      }
      const ok = button("btn dark", "Accept", () =>
        go("accept", { documents: Object.fromEntries(todo.map((d) => [d.id, d.version])) }));
      const sync = () => (ok.disabled = !boxes.every((b) => b.checked));
      boxes.forEach((b) => b.addEventListener("change", sync));
      sync();
      return [list, el("p", "note", "$0 platform fee. No management fee during early access."), ok];
    }
    function linkForm(st) {
      const out = [], l = st.link;
      if (l && l.status === "pending") {
        out.push(el("p", null, `Google Ads account ${l.customer_id}: ${l.instructions}`));
        if (st.link_mode === "api") out.push(button("btn", "Check now", () => go("check")));
      } else if (l && LINK_WORDS[l.status]) out.push(el("p", null, LINK_WORDS[l.status]));
      if (l && l.note) out.push(el("p", "note", l.note));
      const form = el("form", "setup-row"), input = Object.assign(el("input"), {
        name: "customer_id", placeholder: "123-456-7890", inputMode: "numeric", autocomplete: "off", required: true,
      });
      input.setAttribute("aria-label", "Your Google Ads Customer ID");
      form.append(input, Object.assign(el("button", "btn dark", l ? "Use this account" : "Link my Google Ads account"), { type: "submit" }));
      form.addEventListener("submit", (e) => (e.preventDefault(), go("link", { customer_id: input.value })));
      out.push(el("p", null, "Your Google Ads Customer ID — the 10-digit number at the top of your Google Ads page."), form,
        el("p", "note", "Google then shows you an invitation from Admiry inside Google Ads; accept it there. No account yet? Create one at ads.google.com."));
      return out;
    }
    function payForm(st) {
      if (!st.payments_ready) return [el("p", null, "Payments open soon. Nothing to do yet; your plan is saved.")];
      const t = st.topup, form = el("form", "setup-row"), days = el("p", "note"), prices = el("div", "setup-prices");
      const input = Object.assign(el("input"), {
        name: "amount_usd", type: "number", min: String(t.min_usd), max: String(t.max_usd), step: "1", value: String(t.suggested_usd),
      });
      input.setAttribute("aria-label", "Amount in dollars");
      const bank = (t.rails || []).includes("ach");
      const draw = () => {
        const credit = Math.round(Number(input.value || 0) * 100);
        days.textContent = t.daily_usd
          ? `About ${Math.floor(Number(input.value || 0) / t.daily_usd)} days at ${usd(t.daily_usd)} a day. ${usd(t.min_usd)}–${usd(t.max_usd)}.`
          : `${usd(t.min_usd)}–${usd(t.max_usd)}.`;
        const fee = quoteCents(credit, "card", t.rates), card = credit + fee;
        // a price button sits beside the form, not inside it (M1m-1 Task 4 fix round 1): the Enter-key path still
        // submits the form and gets the browser's native check for free, so a click checks the same way first
        // Agree and pay (L1 spec 2026-10-10 §5): a card opened from the plan card's own button names the plan it
        // shows, so the credit approves it once it lands; Add funds (S.more) never names one
        const pay = (rail, label) =>
          button(rail === "card" ? "btn dark" : "btn", label, () => {
            if (!input.reportValidity()) return;
            go("pay", { amount_usd: input.value, rail, ...(o.agree && !S.more && st.plan_id ? { plan_id: st.plan_id } : {}) });
          });
        // P8: the card price is the regular price with its fee inside it; the bank transfer is less than it
        const out = [pay("card", `Pay ${cents(card)} by card`), el("p", "note", `includes a ${cents(fee)} transaction fee`)];
        if (bank) {
          const transfer = credit + quoteCents(credit, "ach", t.rates);
          out.push(
            pay("ach", `Pay ${cents(transfer)} by bank transfer`),
            el("p", "note", `save ${cents(card - transfer)} · arrives in about 4 business days`),
          );
        }
        prices.replaceChildren(...out);
      };
      input.addEventListener("input", draw);
      draw();
      form.append(el("span", "setup-dollar", "$"), input);
      form.addEventListener("submit", (e) => (e.preventDefault(), go("pay", { amount_usd: input.value, rail: "card", ...(o.agree && !S.more && st.plan_id ? { plan_id: st.plan_id } : {}) })));
      const out = [];
      if (st.balance_usd) out.push(el("p", null, `Balance ${money(st.balance_usd)}.`));
      if (st.transfer_pending_usd) {
        out.push(
          el(
            "p",
            null,
            `${cents(Math.round(st.transfer_pending_usd * 100))} is on its way to your balance by bank transfer; it usually arrives in about 4 business days, and Admiry e-mails you when it does. Pay by card only if your ads should start sooner.`,
          ),
        );
      }
      out.push(
        form,
        prices,
        days,
        el("p", "note", "Admiry adds no fee: the transaction fee is what Stripe charges to process the payment, and it is not refunded."),
        el("p", "note", "Spent on the ads at the platform's exact cost. Unspent balance is refundable."),
      );
      return out;
    }
    function standing(st) {
      return st.mode === "managed"
        ? el("p", "setup-done", `✓ Balance ${money(st.balance_usd)}${st.paused ? " — your ads are paused until you add funds" : ""}.`)
        : el("p", "setup-done", `✓ Linked: Google Ads account ${st.link.customer_id}.`);
    }
    function done(st) {
      const out = [standing(st)];
      if (S.launch) {
        // just confirmed: what the launch said, in its words, and where the ads are followed
        out.push(el("p", null, S.launch.say || "Your plan is approved."));
        const fine = el("p", "fine");
        fine.append(Object.assign(el("a", null, "Your ads"), { href: "/ads" }));
        out.push(fine);
      } else if (st.in_review) out.push(el("p", null, st.in_review)); // L1 §6.3: Admiry's review, then live
      else if (st.plan_status === "approved") out.push(el("p", null, "Your plan is approved; its ads are followed on Your ads."));
      else if (st.plan_status === "draft") out.push(el("p", null, "Your plan waits for your yes."));
      // a live link cannot be swapped from here (the server refuses it): only Admiry can move it
      out.push(st.mode === "managed" ? button("btn", "Add funds", () => ((S.more = true), paint()))
        : el("p", "note", "To use a different account, ask Admiry."));
      return out;
    }

    (async () => {
      let r = await go("state");
      const pickable = r && o.pick && offered(r.state).some((m) => m.id === o.pick); // never a way the server left out
      if (pickable && (r.state.step === "choose" || r.state.mode !== o.pick)) r = await go("choose", { mode: o.pick });
      // just back from Stripe: the webhook credits the balance within seconds; follow it (about a minute), and say
      // so if it has not landed by then, so nobody pays twice
      const landed = () => S.state && !(S.state.open_topups > 0) && (S.state.balance_usd || S.state.transfer_pending_usd);
      S.waiting = Boolean(o.paid && r && S.state && S.state.mode === "managed" && !landed());
      if (S.waiting) paint();
      for (let i = 0; S.waiting && i < 20; i++) {
        await new Promise((ok) => setTimeout(ok, 3000));
        if (S.busy) continue; // another of the card's calls on its way (its second look): follow on the next beat
        r = await go("state");
        if (!r && !S.gone) continue; // a failed look (the network, a refusal): still waiting -- follow on the next beat
        S.waiting = Boolean(r && S.state.mode === "managed" && !landed());
        if (!S.waiting && r) paint();
      }
      if (S.waiting) live.textContent = NOT_YET; // still waiting until a reload: no pay form (follow-up final review I2)
      // the money in, or nothing to wait for: the home may draw what sits beside this card now (follow-up R4) -- never
      // while a payment is still being confirmed, so no second pay form appears beside it
      if (r && !S.waiting && o.onSettled) o.onSettled();
    })();
    return {
      refresh() { // a fix made in the conversation: a look on its way is of the plan before it (dropped), and the card
        // looks again -- now, or once the call on its way ends (go)
        S.looks += 1;
        if (S.busy) return (S.again = true), null;
        return look();
      },
    };
  }
  return { run, quoteCents };
})();
