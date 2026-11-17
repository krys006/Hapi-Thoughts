/*
  Address part of a form (the admin owner form and the owner's onboarding
  step 1). The address part of the old static/js/admin/owner_form.js, moved
  here in Slice 8b-3 (same behaviour).
  - The Province select switches between the Town/Barangay dropdowns
    ([data-address-pick]) and the typed fields ([data-address-typed]).
  - After HTMX swaps in a town's barangays, the Barangay select is enabled
    (disabled again when the town is cleared).
  The chosen photo's file name is shown by photo_input.js.
  Without JavaScript both address groups show (a <noscript> style).
*/
(function () {
  "use strict";

  function show(element, visible) {
    if (!element) return;
    element.classList.toggle("contents", visible);
    element.classList.toggle("hidden", !visible);
  }

  function applyProvince(card) {
    var province = card.querySelector("[data-address-province]");
    if (!province) return;
    var other = province.value === "other";
    show(card.querySelector("[data-address-pick]"), !other);
    show(card.querySelector("[data-address-typed]"), other);
    var note = card.querySelector("[data-address-note]");
    if (note) note.hidden = !other;
  }

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target.matches("[data-address-province]")) {
      applyProvince(target.closest("[data-address]"));
    }
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    var select = event.detail.target;
    if (!select.matches("[data-address-barangay]")) return;
    var town = select.closest("[data-address]").querySelector("[data-address-town]");
    select.disabled = !(town && town.value);
  });

  // The browser may restore a changed Province after Back; match it on load
  document.querySelectorAll("[data-address]").forEach(applyProvince);
})();
