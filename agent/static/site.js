"use strict";
// Progressive enhancements shared by the home and Settings (spec 2026-09-29-admiry-settings-redesign §6).
// Every behaviour here is optional: without JS the menu opens and closes, forms submit, text is selectable.
document.addEventListener("DOMContentLoaded", () => {
  // the account menu and Settings' business switcher (native <details>): an outside click closes them; Escape
  // closes them and returns focus
  const menus = [...document.querySelectorAll("details.account-menu, details.switcher")];
  const close = (d) => { if (d.open) { d.open = false; d.querySelector("summary").focus(); } };
  document.addEventListener("click", (e) => menus.forEach((d) => { if (!d.contains(e.target)) d.open = false; }));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") menus.forEach(close); });

  // the home's dock, fixed to the bottom of a phone, rides above the on-screen keyboard: iOS leaves the page's layout
  // under the keyboard and moves only the visual viewport (Android shrinks the page itself: the home's viewport meta
  // asks for interactive-widget=resizes-content). Sticky on a wide screen, where it is left alone.
  const dock = document.getElementById("dock"), vv = window.visualViewport;
  if (dock && vv) {
    const lift = () => (dock.style.transform = getComputedStyle(dock).position !== "fixed" || vv.scale > 1.01 ? ""
      : `translateY(${Math.min(0, vv.height + vv.offsetTop - window.innerHeight)}px)`); // zoomed in: left alone
    ["resize", "scroll"].forEach((t) => vv.addEventListener(t, lift));
  }

  // a form that revokes something asks first
  document.querySelectorAll("form[data-confirm]").forEach((f) =>
    f.addEventListener("submit", (e) => { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); }));

  // Copy buttons: the target's text; the label reads "Copied" for 2 s
  document.querySelectorAll("button[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    const t = document.querySelector(b.dataset.copy);
    if (!t || !navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(t.textContent.trim());
    } catch {
      return; // denied: the text stays selectable
    }
    const was = b.textContent; b.textContent = "Copied"; setTimeout(() => { b.textContent = was; }, 2000);
  }));

  // a folded card the address points at opens: the key-revoke redirect and the nav's Advanced land on #advanced.
  // A click on an in-page link opens it too: clicking the address already in the bar fires no hashchange.
  const unfold = (hash) => {
    const t = hash.length > 1 && document.getElementById(hash.slice(1));
    if (t && t.tagName === "DETAILS") t.open = true;
  };
  unfold(location.hash);
  window.addEventListener("hashchange", () => unfold(location.hash));
  document.addEventListener("click", (e) => {
    const a = e.target.closest('a[href^="#"]');
    if (a) unfold(a.getAttribute("href"));
    if (e.target.closest('a[aria-disabled="true"]')) e.preventDefault(); // the header's links not built yet ("soon")
  });

  // the section nav marks the section in view: the topmost one in the band (80px down to 40% of the window), so two
  // sections in the band at once (a short Overview on a tall window) mark the upper one. The band's contents are
  // kept from every entry: a section that stays in the band sends none, so one batch alone cannot say what is in it.
  // A clicked entry is marked at once and holds until the owner scrolls away from where the jump landed, whichever
  // way they scroll (wheel, touch, keys, the scrollbar, autoscroll): the jump itself passes other sections through
  // the band. At the page's end the last entry is marked, since the short last cards never scroll up into the band.
  const nav = document.querySelector("nav[data-sections]");
  if (nav && "IntersectionObserver" in window) {
    const links = new Map([...nav.querySelectorAll('a[href^="#"]')].map((a) => [a.getAttribute("href").slice(1), a]));
    const last = [...links.keys()].pop();
    const mark = (id) => links.forEach((a, k) => (k === id ? a.setAttribute("aria-current", "true")
      : a.removeAttribute("aria-current")));
    const atEnd = () => window.scrollY > 0
      && window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2;
    let held = null;
    let landed = null; // where the jump ended: at its scrollend, or a second on (no scrollend, or no scroll at all)
    const settle = () => { if (held && landed === null) landed = window.scrollY; };
    nav.addEventListener("click", (e) => {
      const a = e.target.closest('a[href^="#"]');
      if (a) { held = a.getAttribute("href").slice(1); landed = null; mark(held); setTimeout(settle, 1000); }
    });
    window.addEventListener("scrollend", settle);
    ["wheel", "touchmove", "keydown"].forEach((t) => window.addEventListener(t, () => { held = null; }, { passive: true }));
    const inBand = new Set();
    const update = () => {
      if (held) return;
      const id = atEnd() ? last : [...links.keys()].find((k) => inBand.has(k)); // nav order is page order
      if (id) mark(id); // nothing in the band (a gap between cards): the mark stays
    };
    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => (en.isIntersecting ? inBand.add(en.target.id) : inBand.delete(en.target.id)));
      update();
    }, { rootMargin: "-80px 0px -60% 0px" });
    links.forEach((_, id) => { const s = document.getElementById(id); if (s) io.observe(s); });
    window.addEventListener("scroll", () => {
      if (held && landed !== null && Math.abs(window.scrollY - landed) > 40) held = null;
      update();
    }, { passive: true });
  }
});
