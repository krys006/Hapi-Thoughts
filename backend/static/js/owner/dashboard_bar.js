// Owner dashboard top bar (owner/dashboard_parts/top_bar.html).
//   [data-dashboard-band] is the violet band, [data-dashboard-bar] the top bar.
// While any part of the band is still below the bar, the bar gets the
// data-on-band attribute (see-through, white text); once the band has scrolled
// away the attribute is removed and the bar is the normal white one.
// Without JavaScript (or without IntersectionObserver) the bar stays white.
(function () {
  var bar = document.querySelector("[data-dashboard-bar]");
  var band = document.querySelector("[data-dashboard-band]");
  if (!bar || !band || !("IntersectionObserver" in window)) return;

  // The band counts as visible only below the bar's bottom edge
  var margin = "-" + bar.offsetHeight + "px 0px 0px 0px";
  var observer = new IntersectionObserver(
    function (entries) {
      var entry = entries[entries.length - 1];
      bar.toggleAttribute("data-on-band", entry.isIntersecting);
    },
    { rootMargin: margin }
  );
  observer.observe(band);
})();
