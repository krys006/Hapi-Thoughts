document.addEventListener("DOMContentLoaded", function () {
  const bottomNav = document.querySelector(".owner-bottom-nav");

  if (!bottomNav) {
    return;
  }

  let lastScrollY = window.scrollY;

  window.addEventListener("scroll", function () {
    const currentScrollY = window.scrollY;

    if (currentScrollY > lastScrollY && currentScrollY > 120) {
      bottomNav.classList.add("owner-bottom-nav-hidden");
    } else {
      bottomNav.classList.remove("owner-bottom-nav-hidden");
    }

    lastScrollY = currentScrollY;
  });
});