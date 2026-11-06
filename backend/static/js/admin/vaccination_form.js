/*
  Vaccination form (new + edit), loaded by admin/medical/vaccination_form.html.
  Choosing "Other (type it)" in the Vaccine select ([data-vaccine-choice])
  shows the indented "Vaccine name" box ([data-vaccine-other]); any other
  choice hides it (the server clears a leftover typed name on save). The
  server already renders the right state (also after errors); without
  JavaScript the box always shows (a <noscript> style). Same style as
  record_form.js and pet_form.js's species script.
*/
(function () {
  "use strict";

  // Vaccination.OTHER in medical/models.py
  var OTHER = "Other";

  function apply(select) {
    var box = select.form && select.form.querySelector("[data-vaccine-other]");
    if (box) box.classList.toggle("hidden", select.value !== OTHER);
  }

  document.addEventListener("change", function (event) {
    if (event.target.matches("[data-vaccine-choice]")) apply(event.target);
  });

  // The browser may restore a changed select after Back; match it on load
  document.querySelectorAll("[data-vaccine-choice]").forEach(apply);
})();
