// Native <dialog> wiring for admin pages (loaded by layouts/admin.html).
//   <button data-dialog-open="move-dialog">  opens <dialog id="move-dialog">
//   <button data-dialog-close>               inside a dialog closes it
//   click on the dark backdrop               closes the dialog
//   <dialog data-open-on-load>               opens when the page (or an HTMX swap) loads it
// Escape closes natively, and showModal() focuses the [autofocus] element.
(function () {
  // Clicks are handled once on document, so content swapped in by HTMX
  // works without re-binding.
  var pressedOnBackdrop = null;

  // Remember where the press started: dragging to select text in a textarea
  // and releasing outside the box must not close the dialog.
  document.addEventListener("pointerdown", function (event) {
    pressedOnBackdrop = event.target.tagName === "DIALOG" ? event.target : null;
  });

  document.addEventListener("click", function (event) {
    var opener = event.target.closest("[data-dialog-open]");
    if (opener) {
      var dialog = document.getElementById(opener.getAttribute("data-dialog-open"));
      if (dialog && !dialog.open) dialog.showModal();
      return;
    }

    var closer = event.target.closest("[data-dialog-close]");
    if (closer) {
      var parent = closer.closest("dialog");
      if (parent) parent.close();
      return;
    }

    // The backdrop counts as the dialog itself, so target === dialog
    if (event.target.tagName === "DIALOG" && event.target === pressedOnBackdrop) {
      event.target.close();
    }
  });

  // data-open-on-load: open each dialog once, even if htmx:load fires again
  function openOnLoad(root) {
    var dialogs = root.querySelectorAll ? root.querySelectorAll("dialog[data-open-on-load]") : [];
    if (root.matches && root.matches("dialog[data-open-on-load]")) dialogs = [root];
    Array.prototype.forEach.call(dialogs, function (dialog) {
      if (dialog.dataset.openedOnLoad) return;
      dialog.dataset.openedOnLoad = "1";
      if (!dialog.open) dialog.showModal();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { openOnLoad(document); });
  } else {
    openOnLoad(document);
  }
  document.addEventListener("htmx:load", function (event) { openOnLoad(event.target); });
})();
