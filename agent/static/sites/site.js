/* An Admiry industry site's own script (A3p-6 batch 1 Q5): the menu closes on Escape and gives the focus back to its
   button, and the phone bar steps aside while a field is being typed in. Nothing else: the menu works without it
   (a <details>), it never navigates, never opens a window, never sends anything (admiry.js sends the form). */
(function () {
  "use strict";
  var doc = document;
  doc.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    var menus = doc.querySelectorAll("details.menu[open]");
    for (var i = 0; i < menus.length; i++) {
      menus[i].removeAttribute("open");
      var button = menus[i].querySelector("summary");
      if (button) button.focus();
    }
  });
  function typing(el) {
    return el && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) && el.type !== "hidden";
  }
  doc.addEventListener("focusin", function (e) {
    if (typing(e.target)) doc.body.classList.add("typing");
  });
  doc.addEventListener("focusout", function (e) {
    if (typing(e.target)) doc.body.classList.remove("typing");
  });
})();
