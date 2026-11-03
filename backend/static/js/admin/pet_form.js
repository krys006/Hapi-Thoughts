/*
  Pet form (Add pet / Edit pet), loaded by admin/pets/pet_form.html.
  - Species "Other (type it)" shows "Type the species" and the typed breed
    box (labelled "Breed"), and hides the Breed select.
  - Breed "Other (type it)" shows "Type the breed".
  - After HTMX swaps in a species' breeds, the Breed select is enabled
    (disabled again when no species from the list is chosen).
  The chosen photo's file name is shown by photo_input.js.
  Without JavaScript every box shows (a <noscript> style).
*/
(function () {
  "use strict";

  var OTHER = "__other__";

  function show(element, visible) {
    if (element) element.classList.toggle("hidden", !visible);
  }

  // Swaps the label text of the typed breed box, keeping "(optional)"
  function setBreedLabel(wrapper, speciesOther) {
    var input = wrapper.querySelector("[data-breed-text]");
    var label = input && wrapper.querySelector('label[for="' + input.id + '"]');
    if (!label || !label.firstChild) return;
    var text = speciesOther ? wrapper.dataset.labelOther : wrapper.dataset.labelList;
    label.firstChild.nodeValue = "\n      " + text + " ";
  }

  function apply(card) {
    var species = card.querySelector("[data-species-choice]");
    var breed = card.querySelector("[data-breed-choice]");
    if (!species || !breed) return;
    var speciesOther = species.value === OTHER;
    var breedOther = !speciesOther && species.value !== "" && breed.value === OTHER;
    var breedText = card.querySelector("[data-breed-other]");
    show(card.querySelector("[data-species-other]"), speciesOther);
    show(card.querySelector("[data-breed-pick]"), !speciesOther);
    show(breedText, speciesOther || breedOther);
    if (breedText) setBreedLabel(breedText, speciesOther);
  }

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target.matches("[data-species-choice], [data-breed-choice]")) {
      apply(target.closest("[data-pet-about]"));
    }
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    var select = event.detail.target;
    if (!select.matches("[data-breed-choice]")) return;
    var card = select.closest("[data-pet-about]");
    var species = card.querySelector("[data-species-choice]");
    select.disabled = !species.value || species.value === OTHER;
    // The new list starts on "Not sure", so the typed breed box goes away
    apply(card);
  });

  // The browser may restore changed selects after Back; match them on load
  document.querySelectorAll("[data-pet-about]").forEach(apply);
})();
