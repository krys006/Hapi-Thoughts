/*
  Service form (add + edit), loaded by admin/billing/service_form.html.
  The pricing radios show one set of price boxes: "One price" shows
  [data-price-one], "A price range" shows [data-price-range]. The status
  radios show [data-closed-note] only for "No longer offered" (the note
  exists on edit only). The server already renders the right state (also
  after errors); without JavaScript both price sets show (a <noscript>
  style). Same style as record_form.js.
*/
(function () {
  "use strict";

  function show(element, visible) {
    if (element) element.classList.toggle("hidden", !visible);
  }

  function apply(form) {
    var pricing = form.querySelector('input[name="pricing_type"]:checked');
    if (pricing) {
      show(form.querySelector("[data-price-one]"), pricing.value === "fixed");
      show(form.querySelector("[data-price-range]"), pricing.value === "range");
    }
    var status = form.querySelector('input[name="status"]:checked');
    var note = form.querySelector("[data-closed-note]");
    if (status && note) {
      var closed = status.value === "closed";
      show(note, closed);
      // The note is a flex row (icon + text) while it shows
      note.classList.toggle("flex", closed);
    }
  }

  document.addEventListener("change", function (event) {
    if (event.target.matches('input[name="pricing_type"], input[name="status"]')) {
      apply(event.target.closest("[data-service-form]"));
    }
  });

  // The browser may restore changed radios after Back; match them on load
  document.querySelectorAll("[data-service-form]").forEach(apply);
})();
