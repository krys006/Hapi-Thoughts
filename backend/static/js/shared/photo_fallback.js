/*
  Late or broken pictures on the owner pages (Slice 8b-10b-2). Every pet photo
  is an <img data-photo> laid over a tile that already holds the species icon,
  so while the picture is still on its way the icon shows through. If the file
  is missing or broken, the browser fires "error" on the <img>: this adds
  `hidden` to it and the tile with its icon stays. Also marks it
  data-photo-failed, so photo_input.js never shows it again by mistake.
  "error" does not bubble, so the listener is on document in the capture phase.
  Loaded by the owner layouts only. No text, no libraries.
*/
(function () {
  "use strict";

  document.addEventListener(
    "error",
    function (event) {
      var image = event.target;
      if (!image || image.tagName !== "IMG" || !image.matches("img[data-photo]")) return;
      image.setAttribute("data-photo-failed", "");
      image.hidden = true;
    },
    true
  );

  // A picture that failed before this script ran (cached error) is caught here
  document.querySelectorAll("img[data-photo]").forEach(function (image) {
    if (image.complete && image.naturalWidth === 0 && image.getAttribute("src")) {
      image.setAttribute("data-photo-failed", "");
      image.hidden = true;
    }
  });
})();
