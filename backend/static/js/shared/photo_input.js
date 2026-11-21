/*
  Photo cards (admin and owner forms) and the record page's Add file dialog.
  Markup: [data-photo] (the card) holds the [data-photo-input] file input and
  the [data-photo-name] line. Moved from static/js/admin/ in Slice 8b-3.

  Always (every card, as before): the chosen file's name shows in
  [data-photo-name] under the choose button.

  Preview (Slice 8b-10b-2), only in a card that has a [data-photo-tile] with a
  [data-photo-preview] <img> inside it (the owner forms). Words live in the
  templates: data-label-change on the button's label, data-chosen-format on the
  sr-only live line [data-photo-chosen].
  - An image file (type image/*): its picture shows in the tile (an object URL),
    the species icon / initials ([data-photo-base]) and the saved picture
    (img[data-photo]) are hidden, the button's [data-photo-label] changes to its
    data-label-change, and the live line says which photo was chosen. The file
    name is not shown. The old object URL is revoked on the next change and on
    pagehide.
  - If the browser can't draw the picture (a HEIC): the preview is removed, the
    tile is restored and the file name shows as text, as before.
  - Cancelling the dialog (no file) restores the tile.
  - A non-image file: the name shows as before and the tile is restored.
  - [data-photo-clear] (the Remove checkbox on Edit pet): ticking it puts the species
    icon back, drops the preview and clears the file input; choosing a new file
    unticks it (Django refuses a new file together with the clear box).
  A card with no tile (the Add file dialog, which also takes PDFs) only shows
  the name, exactly as before.
*/
(function () {
  "use strict";

  var objectUrls = [];
  var states = new WeakMap(); // card -> {url}

  function partsOf(element) {
    var card = element.closest("[data-photo]");
    if (!card) return null;
    return {
      card: card,
      input: card.querySelector("[data-photo-input]"),
      line: card.querySelector("[data-photo-name]"),
      tile: card.querySelector("[data-photo-tile]"),
      preview: card.querySelector("[data-photo-preview]"),
      live: card.querySelector("[data-photo-chosen]"),
      label: card.querySelector("[data-label-change]"),
      clear: card.querySelector("[data-photo-clear]"),
    };
  }

  function hasTile(parts) {
    return !!(parts.tile && parts.preview);
  }

  function showFileName(input) {
    var card = input.closest("[data-photo]");
    var line = card && card.querySelector("[data-photo-name]");
    if (!line) return;
    var file = input.files && input.files[0];
    line.textContent = file ? "Chosen: " + file.name : "";
    line.hidden = !file;
  }

  function hideFileName(parts) {
    if (!parts.line) return;
    parts.line.textContent = "";
    parts.line.hidden = true;
  }

  function stateOf(card) {
    var state = states.get(card);
    if (!state) {
      state = { url: "" };
      states.set(card, state);
    }
    return state;
  }

  function revoke(state) {
    if (!state.url) return;
    URL.revokeObjectURL(state.url);
    var at = objectUrls.indexOf(state.url);
    if (at !== -1) objectUrls.splice(at, 1);
    state.url = "";
  }

  // mode: "idle" (icon and saved picture), "preview" (the new picture) or
  // "cleared" (the remove box ticked: the icon only)
  function paint(parts, mode) {
    var base = parts.tile.querySelector("[data-photo-base]");
    var saved = parts.tile.querySelector("img[data-photo]");
    if (base) base.hidden = mode === "preview";
    if (saved && !saved.hasAttribute("data-photo-failed")) {
      saved.hidden = mode !== "idle";
    }
    parts.preview.hidden = mode !== "preview";
    if (mode !== "preview") parts.preview.removeAttribute("src");
  }

  function setLabel(parts, changed) {
    var span = parts.label && parts.label.querySelector("[data-photo-label]");
    if (!span) return;
    if (!span.hasAttribute("data-label-original")) {
      span.setAttribute("data-label-original", span.textContent);
    }
    var words = parts.label.getAttribute("data-label-change");
    span.textContent = changed && words ? words : span.getAttribute("data-label-original");
  }

  function say(parts, name) {
    if (!parts.live) return;
    var format = parts.live.getAttribute("data-chosen-format") || "";
    parts.live.textContent = name ? format.replace("{name}", name) : "";
  }

  // Back to the tile as it was: the icon, and the saved picture unless it is being removed
  function restore(parts) {
    revoke(stateOf(parts.card));
    paint(parts, parts.clear && parts.clear.checked ? "cleared" : "idle");
    setLabel(parts, false);
    say(parts, "");
  }

  function choose(parts, file) {
    var state = stateOf(parts.card);
    revoke(state);
    if (parts.clear) parts.clear.checked = false;
    state.url = URL.createObjectURL(file);
    objectUrls.push(state.url);
    parts.preview.src = state.url;
    paint(parts, "preview");
    setLabel(parts, true);
    say(parts, file.name);
    hideFileName(parts);
  }

  function onFileChange(input) {
    var parts = partsOf(input);
    if (!parts) return;
    if (!hasTile(parts)) {
      showFileName(input);
      return;
    }
    var file = input.files && input.files[0];
    if (file && file.type.indexOf("image/") === 0) {
      choose(parts, file);
      return;
    }
    restore(parts);
    showFileName(input);
  }

  function onClearChange(box) {
    var parts = partsOf(box);
    if (!parts || !hasTile(parts)) return;
    if (!box.checked) {
      paint(parts, "idle");
      return;
    }
    if (parts.input) parts.input.value = "";
    hideFileName(parts);
    restore(parts);
  }

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target.matches("[data-photo-input]")) onFileChange(target);
    else if (target.matches("[data-photo-clear]")) onClearChange(target);
  });

  // The browser can't draw this picture (a HEIC): back to the tile, name as text
  document.addEventListener(
    "error",
    function (event) {
      var image = event.target;
      if (!image || !image.matches || !image.matches("img[data-photo-preview]")) return;
      var parts = partsOf(image);
      if (!parts || !hasTile(parts)) return;
      restore(parts);
      if (parts.input) showFileName(parts.input);
    },
    true
  );

  window.addEventListener("pagehide", function () {
    objectUrls.slice().forEach(function (url) {
      URL.revokeObjectURL(url);
    });
    objectUrls = [];
  });
})();
