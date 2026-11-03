/*
  Photo cards (admin owner form and pet form): the chosen file's name shows
  under the "Choose a photo" button. Markup: [data-photo] holds the
  [data-photo-input] file input and the [data-photo-name] line.
*/
(function () {
  "use strict";

  function showFileName(input) {
    var card = input.closest("[data-photo]");
    var line = card && card.querySelector("[data-photo-name]");
    if (!line) return;
    var file = input.files && input.files[0];
    line.textContent = file ? "Chosen: " + file.name : "";
    line.hidden = !file;
  }

  document.addEventListener("change", function (event) {
    if (event.target.matches("[data-photo-input]")) {
      showFileName(event.target);
    }
  });
})();
