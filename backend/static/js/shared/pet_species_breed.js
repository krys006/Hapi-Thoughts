// Cascading species -> breed dropdown with custom input fallback.
// Client-side only — breed lists are static and small, no server
// round-trip needed. Selecting "Other" in either dropdown reveals
// the underlying text input (the actual Django form field) for
// manual entry. No form.py or model changes — this is a JS-enhanced
// layer sitting on top of the existing species/breed text inputs.

const PET_SPECIES_OPTIONS = ["Dog", "Cat", "Bird", "Rabbit", "Hamster"];

const PET_BREEDS_BY_SPECIES = {
  Dog: [
    "Aspin (Native)", "Labrador Retriever", "Shih Tzu", "Chihuahua",
    "Poodle", "Siberian Husky", "Golden Retriever", "Beagle",
    "Pomeranian", "Dachshund", "German Shepherd", "Rottweiler",
    "Shiba Inu", "French Bulldog", "Pug", "Mixed Breed",
  ],
  Cat: [
    "Puspin (Native)", "Persian", "Siamese", "British Shorthair",
    "Maine Coon", "Ragdoll", "American Shorthair", "Scottish Fold",
    "Munchkin", "Mixed Breed",
  ],
  Bird: [
    "Parakeet (Budgerigar)", "Lovebird", "Cockatiel", "Cockatoo",
    "Philippine Hanging Parrot (Colasisi)", "Mynah", "Canary",
  ],
  Rabbit: ["Holland Lop", "Netherland Dwarf", "Rex", "Dutch", "Mixed Breed"],
  Hamster: ["Syrian", "Dwarf Campbell", "Roborovski", "Winter White"],
};

const PET_OTHER_VALUE = "__other__";

/**
 * Initializes the species/breed dropdown pair for one pet form.
 */
function initPetSpeciesBreedFields(speciesInputId, breedInputId) {
  const speciesInput = document.getElementById(speciesInputId);
  const breedInput = document.getElementById(breedInputId);
  if (!speciesInput || !breedInput) return;

  const speciesSelect = document.getElementById(speciesInputId + "_select");
  const breedSelect = document.getElementById(breedInputId + "_select");
  const speciesCustomWrapper = document.getElementById(
    speciesInputId + "_custom_wrapper"
  );
  const breedCustomWrapper = document.getElementById(
    breedInputId + "_custom_wrapper"
  );

  // Explicitly manages both the CSS class (for frontend styling) AND the
  // required attribute + inline display (for guaranteed functional
  // correctness, independent of whether .hidden-field has CSS yet).
  function showCustomInput(wrapper, input, isRequired) {
    wrapper.classList.remove("hidden-field");
    wrapper.style.display = "";
    input.required = isRequired;
  }

  function hideCustomInput(wrapper, input) {
    wrapper.classList.add("hidden-field");
    wrapper.style.display = "none";
    input.required = false;
  }

  function populateBreedOptions(speciesValue, preselectBreed) {
    breedSelect.innerHTML = "";

    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Select a breed";
    breedSelect.appendChild(placeholder);

    const breeds = PET_BREEDS_BY_SPECIES[speciesValue] || [];
    breeds.forEach(function (breed) {
      const option = document.createElement("option");
      option.value = breed;
      option.textContent = breed;
      breedSelect.appendChild(option);
    });

    const otherOption = document.createElement("option");
    otherOption.value = PET_OTHER_VALUE;
    otherOption.textContent = "Other (please specify)";
    breedSelect.appendChild(otherOption);

    if (preselectBreed) {
      const matchFound = breeds.indexOf(preselectBreed) !== -1;
      breedSelect.value = matchFound ? preselectBreed : PET_OTHER_VALUE;
      // Breed is never required=true — model field is optional
      if (matchFound) {
        hideCustomInput(breedCustomWrapper, breedInput);
      } else {
        showCustomInput(breedCustomWrapper, breedInput, false);
      }
    } else {
      hideCustomInput(breedCustomWrapper, breedInput);
    }
  }

  speciesSelect.addEventListener("change", function () {
    if (speciesSelect.value === PET_OTHER_VALUE) {
      showCustomInput(speciesCustomWrapper, speciesInput, true);
      speciesInput.value = "";
      speciesInput.focus();
    } else {
      hideCustomInput(speciesCustomWrapper, speciesInput);
      speciesInput.value = speciesSelect.value;
    }
    // Species changed — breed selection always resets
    populateBreedOptions(speciesSelect.value, null);
    breedInput.value = "";
  });

  breedSelect.addEventListener("change", function () {
    if (breedSelect.value === PET_OTHER_VALUE) {
      // Breed is always optional — required stays false
      showCustomInput(breedCustomWrapper, breedInput, false);
      breedInput.value = "";
      breedInput.focus();
    } else {
      hideCustomInput(breedCustomWrapper, breedInput);
      breedInput.value = breedSelect.value;
    }
  });

  // ── Initial state on page load ─────────────────────────────────────
  // On edit pages, the text inputs already have saved values — try to
  // match them to a predefined option so the dropdowns reflect reality.
  const existingSpecies = speciesInput.value.trim();
  const existingBreed = breedInput.value.trim();

  if (existingSpecies) {
    const speciesMatch = PET_SPECIES_OPTIONS.indexOf(existingSpecies) !== -1;
    speciesSelect.value = speciesMatch ? existingSpecies : PET_OTHER_VALUE;
    if (speciesMatch) {
      hideCustomInput(speciesCustomWrapper, speciesInput);
    } else {
      showCustomInput(speciesCustomWrapper, speciesInput, true);
    }
    populateBreedOptions(speciesMatch ? existingSpecies : "", existingBreed || null);
  } else {
    hideCustomInput(speciesCustomWrapper, speciesInput);
    populateBreedOptions("", null);
  }
}