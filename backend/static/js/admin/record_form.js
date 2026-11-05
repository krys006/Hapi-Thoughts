/*
  Medical record form (new + edit), loaded by admin/medical/record_form.html.
  Ticking "Needs a follow-up visit" shows the Follow-up date block
  ([data-follow-up-on]) and hides the unticked hint ([data-follow-up-off]);
  unticking does the opposite. The server already renders the right state
  (also after errors); without JavaScript the date block always shows (a
  <noscript> style). Same style as pet_form.js's species script.
*/
(function () {
  "use strict";

  function show(element, visible) {
    if (element) element.classList.toggle("hidden", !visible);
  }

  function apply(card) {
    var box = card.querySelector("[data-follow-up-box]");
    if (!box) return;
    show(card.querySelector("[data-follow-up-on]"), box.checked);
    show(card.querySelector("[data-follow-up-off]"), !box.checked);
  }

  document.addEventListener("change", function (event) {
    if (event.target.matches("[data-follow-up-box]")) {
      apply(event.target.closest("[data-follow-up]"));
    }
  });

  // The browser may restore a changed box after Back; match it on load
  document.querySelectorAll("[data-follow-up]").forEach(apply);
})();
