/* The photo upload page (agent/uploads.py, upload.html; the phone redesign of 2026-10-15): a photo chosen in a square
   sends itself -- the square says "Sending…" until the page comes back with the photo in it. Without this script the
   square offers Upload instead (upload.css). Nothing here is inline: the page's CSP lets in this origin's scripts only,
   and every send is still the form's own plain post. */
(function () {
  "use strict";
  var pickers = Array.prototype.slice.call(document.querySelectorAll(".slot > form:not(.rm)"));
  pickers.forEach(function (form) {
    var input = form.querySelector('input[type="file"]');
    if (!input) return;
    input.addEventListener("change", function () {
      var slot = form.closest(".slot");
      if (!input.files || input.files.length === 0 || slot.classList.contains("sending")) return; // one send
      slot.classList.add("sending");
      form.submit();
    });
  });
  // the page shown again from the browser's cache (back from the home, or from the preview) must not still say
  // Sending…, nor hold the photo chosen then
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    Array.prototype.slice.call(document.querySelectorAll(".slot.sending")).forEach(function (slot) {
      slot.classList.remove("sending");
    });
    pickers.forEach(function (form) { form.reset(); });
  });
})();
