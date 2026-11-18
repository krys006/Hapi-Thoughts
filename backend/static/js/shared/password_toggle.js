// Show / Hide password (templates/owner/auth_parts/password_field.html).
//   <button data-password-toggle aria-controls="<input id>"> flips the input
//   between type password and text, and swaps the button's icon, text and
//   aria-pressed. One click listener on document.
(function () {
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-password-toggle]");
    if (!button) return;
    var input = document.getElementById(button.getAttribute("aria-controls"));
    if (!input) return;
    var show = input.type === "password";
    input.type = show ? "text" : "password";
    button.setAttribute("aria-pressed", show ? "true" : "false");
    button.querySelector("[data-label]").textContent = show
      ? "Hide password"
      : "Show password";
    button.querySelector("[data-icon-show]").classList.toggle("hidden", show);
    button.querySelector("[data-icon-hide]").classList.toggle("hidden", !show);
  });
})();
