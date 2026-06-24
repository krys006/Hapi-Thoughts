document.addEventListener("DOMContentLoaded", function () {
  const toggleButtons = document.querySelectorAll("[data-password-toggle]");

  toggleButtons.forEach(function (button) {
    const targetId = button.getAttribute("data-password-toggle");
    const passwordInput = document.getElementById(targetId);

    if (!passwordInput) {
      return;
    }

    button.addEventListener("click", function () {
      const isPassword = passwordInput.type === "password";

      passwordInput.type = isPassword ? "text" : "password";
      button.textContent = isPassword ? "🙈" : "👁";
      button.setAttribute(
        "aria-label",
        isPassword ? "Hide password" : "Show password"
      );
    });
  });
});