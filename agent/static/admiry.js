/* Admiry page script (A3p-1, A3p-3): counts visits, taps, forms and online bookings; keeps what brought the
   visitor (click id, UTM tags, referring site) 90 days in this site's storage; reports one Google Ads conversion per
   contact when the page has the owner's tag. No cookies of its own. Sends only forms meant for Admiry (action
   <base>/t/form/…, or data-admiry="form"; never method="get"); a sent form is counted by the server, never a beacon. */
(function () {
  "use strict";
  var s = document.currentScript; if (!s || !s.dataset.page) return;
  var page = s.dataset.page, base = s.src.replace(/\/t\/admiry\.js.*$/, ""), gads = s.dataset.gads || "";
  var labels = {}; try { labels = JSON.parse(s.dataset.labels || "{}"); } catch (e) {}
  var q = new URLSearchParams(location.search), KEY = "admiry_visit", KEEP = 90 * 864e5, now = Date.now();
  var kinds = ["gclid", "gbraid", "wbraid", "msclkid", "fbclid", "ndclid", "nb_cid", "rdt_cid", "oppref"];
  var tags = ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"];
  // NewsBreak's ads carry its click id as newsbreak_cid; nb_cid (its Pixel's) is the fallback: both kept as nb_cid.
  // Its macro left as written (NewsBreak did not fill it in) is no click id
  function id(v) { return v && v !== "__CALLBACK_PARAM__" ? v : null; }
  function got(k) { return (k === "nb_cid" && id(q.get("newsbreak_cid"))) || id(q.get(k)); }
  function some(o) { return Object.keys(o || {}).length > 0; }
  var ids = {}, utm = {}, ref = "", kept = null;
  try { ref = new URL(document.referrer).hostname; } catch (e) {}
  if (ref === location.hostname) ref = "";
  kinds.forEach(function (k) { var v = got(k); if (v) ids[k] = v; });
  tags.forEach(function (k) { var v = q.get(k); if (v) utm[k] = v; });
  try { kept = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) {}
  if (!kept || !(now - kept.t < KEEP)) kept = null; // what brought the visitor is remembered 90 days
  // the last click wins; a tagged link or another site replaces only what has no click; a direct visit keeps it
  var visit = kept || { ids: {}, utm: {}, ref: "" };
  if (some(ids) || ((some(utm) || ref) && !(kept && some(kept.ids)))) {
    visit = { ids: ids, utm: utm, ref: ref, t: now };
    try { localStorage.setItem(KEY, JSON.stringify(visit)); } catch (e) {}
  }
  function carry(set, pre) {
    [visit.ids, visit.utm].forEach(function (o) { Object.keys(o || {}).forEach(function (k) { set(pre + k, o[k]); }); });
    if (visit.ref) set(pre + "ref", visit.ref);
  }
  if (gads) {
    var g = document.createElement("script"); g.async = true;
    g.src = "https://www.googletagmanager.com/gtag/js?id=" + encodeURIComponent(gads); document.head.appendChild(g);
    window.dataLayer = window.dataLayer || []; window.gtag = window.gtag || function () { window.dataLayer.push(arguments); };
    window.gtag("js", new Date());
    // a call from the site (P26): Google's forwarding number for a visitor from its ad
    if (labels.call_site && s.dataset.phone)
      window.gtag("set", { phone_conversion_number: s.dataset.phone, phone_conversion_ids: [gads + "/" + labels.call_site] });
    window.gtag("config", gads, { cookie_domain: location.hostname });
  }
  function conversion(kind) { if (gads && labels[kind]) window.gtag("event", "conversion", { send_to: gads + "/" + labels[kind] }); }
  function beacon(kind) {
    var body = new URLSearchParams({ page: page, kind: kind, url: location.href });
    carry(function (k, v) { body.set(k, v); }, "");
    var ok = false; try { ok = navigator.sendBeacon(base + "/t/event", body); } catch (e) {}
    if (!ok) fetch(base + "/t/event", { method: "POST", body: body, mode: "no-cors", keepalive: true }).catch(function () {});
    conversion(kind);
  }
  beacon("visit");
  document.addEventListener("click", function (e) {
    var a = e.target && e.target.closest ? e.target.closest("a, button") : null; if (!a) return;
    var h = (a.getAttribute("href") || "").toLowerCase(), k = a.dataset.admiry;
    if (k === "call" || h.indexOf("tel:") === 0) beacon("call");
    else if (k === "mail" || h.indexOf("mailto:") === 0) beacon("mail");
    else if (k === "book") beacon("book");
  }, true);
  document.addEventListener("submit", function (e) {
    var f = e.target; if (!f || f.tagName !== "FORM" || (f.getAttribute("method") || "").toLowerCase() === "get") return;
    var to = f.getAttribute("action") || "";
    try { to = to && new URL(to, location.href).href; } catch (x) { to = ""; }
    if (to.indexOf(base + "/t/form/") !== 0) to = f.getAttribute("data-admiry") === "form" ? base + "/t/form/" + encodeURIComponent(page) : "";
    if (!to) return;
    function hidden(k, v) {
      if (!f.elements[k]) { var i = document.createElement("input"); i.type = "hidden"; i.name = k; i.value = v; f.appendChild(i); }
    }
    carry(hidden, "_");
    // the form's intent (P25): consult, appointment or quote; the answer names its one conversion
    var intent = f.getAttribute("data-admiry-intent"); if (intent) hidden("_intent", intent);
    e.preventDefault();
    var btn = f.querySelector("[type=submit]"); if (btn) btn.disabled = true;
    fetch(to, { method: "POST", body: new FormData(f), headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && j.ok) { if (j.lead) conversion(j.conversion || "form"); var p = document.createElement("p"); p.className = "admiry-thanks"; p.textContent = j.say; f.replaceWith(p); }
        else { if (btn) btn.disabled = false; alert(j && j.say ? j.say : "Please try again."); }
      })
      .catch(function () { if (btn) btn.disabled = false; });
  }, true);
  // a booking completed in a Calendly embed (P27), once a page view
  var booked = false;
  window.addEventListener("message", function (e) {
    if (!booked && e.origin === "https://calendly.com" && e.data && e.data.event === "calendly.event_scheduled") { booked = true; beacon("booked_online"); }
  });
})();
