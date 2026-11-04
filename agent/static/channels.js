/* The channels on the homepage (spec 2026-09-29 §7.2), drawn for home.js: the plan card's rows (one per funded
   channel, then the later lines), a budget level's split in the owner's names for the channels (channels.registry's
   LABELS, D76), the picture a channel shows with the ad (M1 Task 7), and the channels card at the top of the plan once
   the ads run (docs/superpowers/mocks/channels-card.html). Loaded before home.js, like setup.js; nothing from
   elsewhere, and every dynamic string goes in with textContent. */
"use strict";
window.AdmiryChannels = (() => {
  const doc = document;
  const el = (tag, cls, text) =>
    Object.assign(doc.createElement(tag), cls ? { className: cls } : {}, text != null ? { textContent: text } : {});
  const part = (root, name) => root.querySelector(`[data-part="${name}"]`);
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
  const usd = (n) => "$" + Math.round(Number(n)).toLocaleString("en-US");
  const LABELS = { google_search: "Google Search", newsbreak: "NewsBreak", microsoft_search: "Bing Search",
    meta: "Facebook & Instagram", nextdoor: "Nextdoor", lsa: "Google Local Services", reddit: "Reddit",
    chatgpt_ads: "ChatGPT" }; // channels.registry.LABELS (D76), for a level's split

  // ------------------------------------------------------------------ the plan card's rows, a level's split
  function channel(label, why, role, meta) {
    const li = el("li", role === "later" ? "later" : ""), text = el("div");
    text.append(el("p", "ch-name", label), el("p", "ch-why", why));
    if (meta) text.append(el("p", "ch-meta", meta));
    li.append(text, el("span", `tag ${role}`, role));
    return li;
  }
  function planRows(ul, p) { // one row per funded channel -- its amount and who runs it -- then the later lines
    for (const ch of p.channels || []) {
      ul.append(channel(ch.label, ch.why, ch.role || "primary", `${usd(ch.monthly_usd)} a month · ${ch.mode_text}`));
    }
    for (const l of p.later || []) ul.append(channel(l.label, l.why, "later"));
  }
  // A level's split across the channels, "Google Search $1,900 · NewsBreak $300"; nothing when one channel has it all.
  const split = (amounts) => (Object.keys(amounts || {}).length > 1
    ? Object.entries(amounts).map(([ch, amount]) => `${LABELS[ch] || ch} ${usd(amount)}`).join(" · ") : "");
  // The ad preview's picture (plan P7): the one a channel shows with the ad (NewsBreak), with the manifest's words for a
  // screen reader, and the line under it -- approving the plan approves it, or the channel waits for one. Only a web
  // address becomes a picture; a plan whose channels show none has no figure.
  function picture(fig, p) {
    if (!p.image_note) return fig.remove();
    const url = p.image && typeof p.image.url === "string" && /^https?:\/\//.test(p.image.url) ? p.image.url : null;
    if (url) fig.append(Object.assign(el("img"), { src: url, alt: p.image.alt || "", loading: "lazy" }));
    fig.append(el("figcaption", "note", p.image_note));
  }

  // ------------------------------------------------------------------ the channels card
  // MarketingState's rows, each a state dot and one sentence, today's spend while the ads spend, this week's calls and
  // forms, and its one action; the footer; and the money block's mount, which M1m fills. `o` is home.js's side:
  // business() (the Your ads page's links and the Pause/Resume form), and for the Pause/Resume: work (the page's busy
  // state), csrf, post, turn, draw (the plan drawn again from the `state` answer), oops and announce.
  const TONES = { live: "on", limited: "wait", paused: "off", failed: "bad", waiting_account: "wait",
    waiting_creative: "wait", operator: "on" };
  const money = (n) => (Number.isInteger(Number(n)) ? usd(n)
    : "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const count = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const adsAt = (o) => (o.business() ? `/ads?b=${encodeURIComponent(o.business())}` : "/ads");
  function card(ch, o) {
    const n = doc.getElementById("t-channels").content.firstElementChild.cloneNode(true);
    const ul = part(n, "rows"), f = ch.footer || {};
    part(n, "settings").href = adsAt(o);
    for (const r of ch.rows) ul.append(row(r, o));
    const day = f.next_report_at && new Date(`${f.next_report_at}T12:00:00`).toLocaleDateString("en-US", { weekday: "long" });
    part(n, "footer").textContent = `Month to date ${money(f.month_usd || 0)}` +
      (f.month_cap_usd != null ? ` of ${money(f.month_cap_usd)}` : "") + (day ? ` · next report ${day}` : "");
    return n;
  }
  function row(r, o) {
    const li = el("li", "where-row"), text = el("div", "w-text");
    const dot = el("span", `dot ${(r.issues || []).some((i) => i.since) ? "bad" : TONES[r.state] || "busy"}`);
    dot.setAttribute("aria-hidden", "true");
    const week = [r.calls_7d ? count(r.calls_7d, "call") : null, r.leads_7d ? count(r.leads_7d, "form") : null];
    const meta = [["live", "limited"].includes(r.state) && r.today_usd != null && r.day_cap_usd != null
      ? `${money(r.today_usd)} of ${money(r.day_cap_usd)} today` : null,
    week.some(Boolean) ? `${week.filter(Boolean).join(", ")} this week` : null].filter(Boolean);
    text.append(el("p", "w-name", r.label), el("p", "w-state", r.state_text));
    if (meta.length) text.append(el("p", "w-meta", meta.join(" · ")));
    li.append(dot, text);
    for (const a of r.actions || []) { // each names its channel to a screen reader, as Settings' buttons do
      const b = a === "connect" ? Object.assign(el("a", "ghost", "Connect"), { href: `${adsAt(o)}#${r.channel}` })
        : Object.assign(el("button", "ghost", cap(a)), { type: "button" });
      if (a !== "connect") b.addEventListener("click", () => press(a, r, o));
      b.append(el("span", "sr", ` on ${r.label}`));
      li.append(b);
    }
    return li;
  }
  function press(action, r, o) { // Settings' own Pause/Resume (the owner's click, CSRF), answered in JSON
    o.work(async () => {
      const token = await o.csrf();
      const j = token ? await o.post(`/connect/ads/${action}`, { business_id: o.business(), channel: r.channel, csrf: token }, true)
        : { ok: false, error: "Sign in first." };
      if (!j.ok) return o.oops(j.error);
      const st = await o.turn("state");
      if (st.ok && st.result && st.result.cards) o.draw(st.result);
      o.announce(`${r.label}: ${action === "pause" ? "paused" : "resumed"}.`);
    });
  }

  return { planRows, split, picture, card };
})();
