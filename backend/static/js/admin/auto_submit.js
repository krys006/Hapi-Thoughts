// Submit a GET form as soon as a control changes (loaded by layouts/admin.html).
//   <select name="show" data-auto-submit>   picking an option reloads the page
// Keep a submit button inside <noscript> so the form works without JavaScript.
(function () {
  // Handled once on document, so forms swapped in by HTMX work too.
  document.addEventListener("change", function (event) {
    var control = event.target.closest("[data-auto-submit]");
    if (!control || !control.form) return;
    if (control.form.requestSubmit) control.form.requestSubmit();
    else control.form.submit();
  });
})();
