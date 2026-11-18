// Flash messages (components/flash_messages.html), loaded by both layouts.
//   <button data-flash-close> inside a [data-flash] box removes that message.
// Messages never hide on their own: they stay until closed.
// One click listener on document, so messages swapped in later also work.
(function () {
  document.addEventListener("click", function (event) {
    var closer = event.target.closest("[data-flash-close]");
    if (!closer) return;
    var message = closer.closest("[data-flash]");
    if (message) message.remove();
  });
})();
