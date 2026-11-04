/* Admiry's own site on the homepage's page card (A3p-6 batch 2 Q17, Q18), drawn for home.js from the site's state
   (`/api/page/state`, `kind: "site"`): the owner picks a look from the samples' screenshots and answers the facts still
   missing on one card; then sees the site at three sizes with its photo checklist, the phone link for the photos, the
   wordmark, the industry's own facts, another look, and Publish, held until the logo and the key photo are in; once it
   is live, where. Every answer of the server draws the card again, its heading in view and focused; a refusal is a
   sentence on it, in view and focused. Loaded after channels.js and before home.js; nothing from elsewhere, and every
   dynamic string goes in with textContent. */
"use strict";
window.AdmirySites = (() => {
  const SIZES = ["phone", "tablet", "desktop"];
  const BUSY = "Building your site…";
  const AGAIN = "Build it again"; // home_api.BUILD_AGAIN, and build.REBUILD_FIRST's sentence below, word for word
  const REBUILD = "Your photos are in; build the site once more, then publish.";
  const AUTO = { name: "organization", phone: "tel" }; // the facts form's fields a browser can fill
  const signedOut = (r) => r.signin || r.wall || r.fresh; // home.js's own answers to these (handle)

  // ------------------------------------------------------------------ the card
  // `p` is the site's state (home_api._site_state): where it stands (tools_sites.site_view), its screenshots by size,
  // the question of each fact still missing, the industry's own facts and the build button's words. `api` is home.js's
  // side: post, csrf, work (the page's busy state), handle, announce, safeHref, el, clone, part, sentence, reveal and
  // checks (a page's list of checks drawn into the card).
  // A fact still missing is asked, with the looks, whatever the status: a build that asks never ends the card. Returns
  // the card's heading, which a redraw brings into view and focus (`bring`); the page's own load does not.
  function card(host, p, api) {
    const n = api.clone("t-site"), own = p.status === "none" && api.safeHref(p.own_site_url);
    const asking = !own && (p.status === "none" || (p.missing || []).length > 0);
    host.replaceChildren(n);
    if (own) ownSite(host, n, p, api, own);
    if (asking) pick(host, n, p, api);
    const top = p.status === "none" ? null : built(host, n, p, api, asking);
    notes(n, p, api); // the hosting minimum said before the site is built too, as the page card's intro said it
    return own ? api.part(n, "own-say") : asking ? api.part(n, "lead") : top;
  }
  // The ads send people to the owner's own website (2026-10-08): where, and Admiry's site instead -- the looks again.
  function ownSite(host, n, p, api, url) {
    const box = api.part(n, "own");
    box.hidden = false;
    api.part(n, "own-say").textContent = `Your ads send people to your website, ${url}.`;
    api.part(n, "own-instead").addEventListener("click", () => {
      box.hidden = true;
      pick(host, n, p, api);
      bring(api, api.part(n, "lead"));
    });
  }
  function bring(api, node) { // after a redraw or a refusal: in view, and where the keyboard and a screen reader are
    api.reveal(node);
    node.focus({ preventScroll: true });
  }
  // A build, or the industry's facts skipped: the button says what is under way, the card is drawn again from the
  // answer and its sentence read out -- or, refused (the lock's sentence, a fact refused, the build failed), the card
  // stays with what was typed and says why. Sign-in and the wall are the page's (handle).
  function send(host, p, api, args, button, path = "/api/site/build") {
    return api.work(async () => {
      const label = button && button.textContent;
      if (button) button.textContent = BUSY;
      const r = await api.post(path, { csrf: await api.csrf(), ...args });
      if (button) button.textContent = label;
      if (r.ok && r.page) {
        bring(api, card(host, r.page, api));
        return r.say && api.announce(r.say);
      }
      if (signedOut(r)) return api.handle(r);
      said(host, api, r.error);
    });
  }
  function said(host, api, text) { // a refusal, as a sentence on the card, in view and focused, and read out
    const note = api.part(host, "site-error");
    note.textContent = api.sentence(text);
    note.hidden = false;
    bring(api, note);
    api.announce(note.textContent);
  }

  // ------------------------------------------------------------------ before the first build: the look, the facts
  function looks(box, list, chosen, tap, api) { // each look its sample's phone screenshot, its name and its line
    box.replaceChildren(...list.map((look) => {
      const b = Object.assign(api.el("button", "look"), { type: "button" });
      const img = Object.assign(api.el("img"), { src: api.safeHref(look.look_url) || "", alt: "", loading: "lazy" });
      b.dataset.look = look.id;
      b.setAttribute("aria-pressed", String(look.id === chosen));
      b.append(img, api.el("b", null, look.name), api.el("span", null, look.line));
      b.addEventListener("click", () => tap(look.id));
      return b;
    }));
  }
  // The three looks; a tap builds the site in it at once, or -- facts still missing -- opens them on one card first,
  // each labelled with its question (the services a box of lines), sent with the look by the build button. A look kept
  // with nothing missing (a build that failed) keeps the button too, to build again.
  function pick(host, n, p, api) {
    const part = (name) => api.part(n, name), form = part("facts"), build = part("build");
    let chosen = p.style; // the look the draft keeps (a build asked a fact, or failed), until the owner taps another
    part("pick").hidden = false;
    const facts = p.missing.map((id) => ({ id, label: p.prompts[id] || id, needed: true,
      input: id === "services" ? "textarea" : "text", type: id === "phone" ? "tel" : null }));
    const read = ask(api, facts, part("fields"));
    const tap = (id) => {
      chosen = id;
      looks(part("looks"), p.looks, chosen, tap, api);
      form.hidden = false;
      if (!p.missing.length) return send(host, p, api, { style: id }, build);
      const first = form.querySelector("input, textarea");
      if (first) first.focus({ preventScroll: true });
    };
    looks(part("looks"), p.looks, chosen, tap, api);
    build.textContent = p.build_label;
    form.hidden = !chosen;
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (chosen) send(host, p, api, { style: chosen, facts: read() }, build);
    });
    const own = part("own-form"), typed = own.querySelector("input");
    own.addEventListener("submit", (e) => { // "Already have a website?": the ads' page, then the page drawn again
      e.preventDefault();
      const url = String(typed.value || "").trim();
      if (!url) return;
      api.work(async () => {
        const r = await api.post("/api/page/own-site", { csrf: await api.csrf(), url });
        if (signedOut(r)) return api.handle(r);
        if (!r.ok) return said(host, api, r.error);
        api.announce(r.say);
        api.reload(); // the plan's "Your website" card, with its ways to add Admiry's code, comes with the page
      });
    });
  }
  // The fields for `list` appended to `box`; returns read(): the answers given, as the facts build_site takes (one left
  // empty says nothing).
  function ask(api, list, box) {
    const fields = list.map((e) => [e.id, field(api, e)]);
    box.append(...fields.map(([, f]) => f.node));
    return () => Object.fromEntries(fields.map(([id, f]) => [id, f.read()]).filter(([, v]) => v !== undefined));
  }
  // One answer, labelled: a line, a box of lines (`list`: lines or commas, read by the server), yes or no -- or a fact
  // of a few parts (`fields`: the dentist, the social pages; a review is a list of one), those `needed` asked for once
  // any part is given. A `link` is read as an https address.
  function field(api, e) {
    if (e.fields) {
      const set = api.el("fieldset", "site-field");
      const parts = e.fields.map((f) => field(api, { ...f, id: `${e.id}.${f.id}` })); // named "reviews.text", ...
      set.append(api.el("legend", null, e.label), ...parts.map((f) => f.node));
      const read = () => {
        const given = e.fields.map((f, i) => [f.id, parts[i].read()]).filter(([, v]) => v !== undefined);
        const got = Object.fromEntries(given);
        return Object.keys(got).length ? (e.input === "review" ? [got] : got) : undefined;
      };
      const asked = () => parts.forEach((f, i) => (f.input.required = Boolean(e.fields[i].needed && read())));
      set.addEventListener("input", asked);
      return { node: set, read, input: set };
    }
    const kind = e.input || "text", label = api.el("label", "site-field");
    const input = api.el(kind === "textarea" || kind === "list" ? "textarea" : kind === "yesno" ? "select" : "input");
    for (const [v, t] of kind === "yesno" ? [["", ""], ["yes", "Yes"], ["no", "No"]] : []) {
      input.append(Object.assign(api.el("option", null, t), { value: v }));
    }
    if (input.tagName === "TEXTAREA") input.rows = 3;
    if (e.type && e.type !== "link") input.type = e.type;
    input.name = e.id;
    if (AUTO[e.id]) input.autocomplete = AUTO[e.id];
    if (e.needed && !e.id.includes(".")) input.required = true; // the facts a site cannot be built without
    label.append(api.el("span", null, e.label), input);
    const read = () => {
      const v = String(input.value || "").trim();
      if (!v) return undefined;
      if (kind === "yesno") return v === "yes";
      return e.type === "link" && !/^https:\/\//i.test(v) ? `https://${v.replace(/^http:\/\//i, "")}` : v;
    };
    return { node: label, read, input };
  }

  // ------------------------------------------------------------------ the site
  // Its screenshots and address -- or, its preview ended, the sentence and "Build it again" (the words are kept: no
  // model call); and before it is live, the photos, the industry's facts, another look and Publish.
  // A live site whose preview is newer (photos or a look since it went live) shows its photos and Publish too, above
  // the published line, and a link to the new version. Returns the heading: ready to look over, its preview ended, or
  // live.
  function built(host, n, p, api, asking) {
    const part = (name) => api.part(n, name), live = p.status === "published";
    const href = api.safeHref(live ? p.published && p.published.url : p.preview && p.preview.url);
    const top = part(live ? "live" : p.preview ? "ready" : "expired");
    part("built").hidden = top.hidden = false;
    shots(n, p, api);
    const hours = part("hours-shown"), lines = p.hours_shown || []; // the hours as the words wrote them, to check
    hours.textContent = lines.length ? `Opening hours on your site: ${lines.join("; ")}.` : "";
    hours.hidden = !lines.length;
    const open = part("open");
    open.hidden = !href;
    if (href) open.href = href;
    if ((!live && !p.preview) || p.rebuild_needed) again(host, n, p.build_label, api);
    const newer = part("newer"), next = live && p.newer_preview && api.safeHref(p.preview && p.preview.url);
    newer.hidden = !next;
    if (next) newer.href = next;
    if (!live || p.newer_preview) checklist(host, n, p, api), publish(host, n, p, api);
    if (live) return top;
    extras(host, n, p, api);
    const relook = part("relook"), summary = relook.querySelector("summary"); // it says the build is under way
    relook.hidden = asking; // the looks are already up top, with the fact asked
    looks(part("relooks"), p.looks, p.style, (id) => id !== p.style && send(host, p, api, { style: id }, summary), api);
    return top;
  }
  // "Build it again" -- no look, no facts: the words kept unless a fact they read changed -- for a preview that
  // ended, or one that lacks the photos on file (the state's `rebuild_needed`, or a Publish refused with that
  // sentence). Wired once.
  function again(host, n, label, api) {
    const b = api.part(n, "again");
    if (!b.hidden) return;
    b.hidden = false;
    b.textContent = label;
    b.addEventListener("click", () => send(host, null, api, {}, b));
  }
  // Phone / Tablet / Computer over one screenshot of the site, the first size with one pressed; none without the render
  // check (the card then has "Open your site" alone).
  function shots(n, p, api) {
    const tabs = api.part(n, "tabs"), img = api.part(n, "shot"), buttons = tabs.querySelectorAll("[data-size]");
    const have = SIZES.filter((size) => api.safeHref((p.shots || {})[size]));
    const show = (size) => {
      img.src = p.shots[size];
      img.dataset.size = size; // a tablet's or a computer's picture is wider than a phone's
      buttons.forEach((t) => t.setAttribute("aria-pressed", String(t.dataset.size === size)));
    };
    tabs.hidden = img.hidden = !have.length;
    buttons.forEach((t) => {
      t.hidden = !have.includes(t.dataset.size);
      t.addEventListener("click", () => show(t.dataset.size));
    });
    if (have.length) show(have[0]);
  }
  // As the page card has them: "Published at …" with the last check's count and the checks listed under it (home.js's
  // own list, `api.checks`), the hosting minimum's sentence or the note before publishing, the Muse invitation once
  // live, the download once Admiry hosts it.
  function notes(n, p, api) {
    const status = api.part(n, "status"), pub = p.status === "published" && p.published;
    if (pub) {
      const rules = (pub.check && pub.check.rules) || [], passed = rules.filter((r) => r.ok === true).length;
      const count = rules.length ? ` (${passed} of ${rules.length} checks pass)` : "";
      status.textContent = `Published at ${pub.url}${count}`;
    }
    status.hidden = !pub;
    api.checks(n, pub ? pub.check : null);
    const hosting = api.part(n, "hosting"), text = (p.hosting && p.hosting.say) || p.hosting_note || "";
    hosting.textContent = text;
    hosting.hidden = !text;
    const muse = api.part(n, "muse");
    api.part(muse, "muse-text").textContent = p.muse || "";
    muse.hidden = !p.muse;
    const download = api.part(n, "download"), href = api.safeHref(p.download_url);
    download.hidden = !href;
    if (href) download.href = href;
  }
  // What to upload, most important first: each kind, how many are in of how many the site uses, a check once done, and
  // the two the site waits for marked; the phone link; the business name as the logo while there is no logo.
  function checklist(host, n, p, api) {
    api.part(n, "photos").hidden = false;
    api.part(n, "checklist").replaceChildren(...p.checklist.map((item) => {
      const li = api.el("li", item.done ? "done" : "todo");
      li.append(item.done ? api.clone("t-check") : api.el("span", "hollow"), api.el("span", null, item.label),
        api.el("span", "count", `${item.have} of ${item.need}`));
      if (item.blocks_publish) li.append(api.el("span", "tag", "Needed to publish"));
      return li;
    }));
    const upload = api.part(n, "upload"), href = api.safeHref(p.upload_url);
    upload.hidden = !href;
    if (href) upload.href = href;
    const logo = p.checklist.find((item) => item.id === "logo"), mark = api.part(n, "wordmark");
    mark.hidden = !logo || logo.done || Boolean(p.key_photos && p.key_photos.wordmark_ok);
    mark.addEventListener("click", () => send(host, p, api, { wordmark: true }, mark));
  }
  // The industry's own facts, once (Q4): given -- sent as facts, the site built with them -- or skipped.
  function extras(host, n, p, api) {
    const box = api.part(n, "extras");
    box.hidden = Boolean(p.extras_asked) || !(p.extras || []).length;
    if (box.hidden) return;
    const read = ask(api, p.extras, api.part(n, "extras-fields")), button = api.part(n, "extras-send");
    api.part(n, "extras-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const facts = read();
      if (Object.keys(facts).length) send(host, p, api, { facts }, button);
    });
    const skip = () => send(host, p, api, { skipped: true }, null, "/api/site/extras");
    api.part(n, "skip").addEventListener("click", skip);
  }
  // Publish, held while the logo or the key photo is missing -- aria-disabled, not disabled: the page's busy state
  // gives every button back when it ends -- with the gate's sentence beneath (its description), read out on a tap.
  // Published or refused, the card is drawn again from the state; a refusal (the gate, a rebuild needed first) is then
  // a sentence on it -- a rebuild needed with "Build it again" beside it -- but a preview that ended (24 hours, or a
  // photo made a newer one) is the card's own news: drawn again, it shows the newer preview or "Build it again".
  function publish(host, n, p, api) {
    const b = api.part(n, "publish"), gate = api.part(n, "gate"), held = !(p.key_photos && p.key_photos.ok);
    b.hidden = !p.preview;
    b.setAttribute("aria-disabled", String(held));
    gate.textContent = held ? (p.key_photos && p.key_photos.sentence) || "" : "";
    gate.hidden = b.hidden || !gate.textContent;
    b.addEventListener("click", () => {
      if (b.getAttribute("aria-disabled") === "true") return api.announce(gate.textContent);
      api.work(async () => {
        const r = await api.post("/api/page/publish", { csrf: await api.csrf(), token: p.preview.token });
        if (!r.ok && signedOut(r)) return api.handle(r);
        const s = await api.post("/api/page/state", { csrf: await api.csrf() }); // where the site stands now
        const top = s.ok && s.page ? card(host, s.page, api) : null;
        if (r.ok) {
          if (top) bring(api, top);
          if (api.published) api.published(); // B0: what stops the launch may be gone
          return api.announce(r.say || `Published at ${r.page.url}.`); // the check's and the ads' sentences with it
        }
        const ended = top && api.sentence(r.error) === api.part(host, "expired").textContent;
        if (ended) return bring(api, top);
        if (r.error === REBUILD) again(host, host, AGAIN, api);
        return said(host, api, r.error);
      });
    });
  }

  return { card };
})();
