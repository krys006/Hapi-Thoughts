/*
  Free-times area of the owner pages (Slice 8b-10b-3): booking step 2 and Change
  day or time. Loaded by layouts/owner.html. Plain browser JavaScript, no
  libraries. The month arrows and open days are links that also carry hx-get;
  HTMX swaps only #day-area (owner/booking_parts/_day_area.html). Every visible
  word lives in that template and in booking_parts/day_states.html: this file has
  no English text. It does nothing on a page without #day-area.

  Own file, not list_states.js: that one locks a select ([data-list-field]) and
  retries by sending change; this area is a set of links, so it needs its own
  trigger, its own states and the Next / Send guard. The approach is the same:
  one listener per event on document, the failure events, a retry button that
  sends the same request again, every element optional.

  - A request starts (htmx:beforeRequest): the area is aria-busy, the boxes are
    hidden, the name of the day or month (data-day-label of the tapped link) is
    written into [data-day-name] with textContent, and the form's primary
    button (Next / Send new time) is blocked. The loading line and the grey
    boxes are shown by CSS while htmx has the htmx-request class on the area.
  - It works (htmx:afterSwap): the area was replaced with fresh content, so
    every state is gone; the button is given back.
  - It fails (htmx:timeout after 10 s, htmx:responseError, htmx:sendError, also
    offline): nothing was swapped. data-day-failed on the area keeps the grey
    boxes (dimmed) and hides the old times; the matching [data-day-error] box
    shows. The button stays blocked, because the old times are hidden.
  - [data-day-retry] sends the same request again: the link that started it is
    clicked again by HTMX and the box is hidden.
  - Blocked means aria-disabled plus the muted look of [data-busy-muted], and a
    capture submit guard: while the area loads or has failed, a submit of its
    form does nothing (so a stale time can never be sent).
  - pageshow and htmx:historyRestore clear everything (a page back from the
    back/forward cache is never left loading).
*/
(function () {
  "use strict";

  var AREA_ID = "day-area";
  var lastLink = null; // the link that started the request the retry repeats
  var retrying = false;

  function area() {
    return document.getElementById(AREA_ID);
  }

  function linkOf(event) {
    var detail = event.detail || {};
    var element = detail.elt || event.target;
    var link = element && element.closest ? element.closest("[data-day-label]") : null;
    return link && link.closest("#" + AREA_ID) ? link : null;
  }

  function isLoading(box) {
    return box.classList.contains("htmx-request");
  }

  function isFailed(box) {
    return box.hasAttribute("data-day-failed");
  }

  // The form's one violet button (Next / Send new time) lives outside the area
  function primaryButton(box) {
    var form = box.querySelector("form");
    if (!form || !form.id) return null;
    try {
      return document.querySelector('button[form="' + form.id + '"]');
    } catch (problem) {
      return null;
    }
  }

  function block(box) {
    var button = primaryButton(box);
    if (!button) return;
    button.setAttribute("aria-disabled", "true");
    button.setAttribute("data-busy-muted", "");
    button.setAttribute("data-day-blocked", "");
  }

  function unblock(box) {
    var button = primaryButton(box);
    if (!button || !button.hasAttribute("data-day-blocked")) return;
    button.removeAttribute("data-day-blocked");
    button.removeAttribute("data-busy-muted");
    button.removeAttribute("aria-disabled");
  }

  function hideErrors(box) {
    Array.prototype.forEach.call(
      box.querySelectorAll("[data-day-error]"),
      function (element) {
        element.hidden = true;
      }
    );
  }

  function showError(box, kind) {
    hideErrors(box);
    var element = box.querySelector('[data-day-error="' + kind + '"]');
    if (!element) return null;
    element.hidden = false;
    return element;
  }

  // Back to the quiet state: nothing loading, nothing failed
  function clear(box) {
    box.setAttribute("aria-busy", "false");
    box.removeAttribute("data-day-failed");
    box.classList.remove("htmx-request");
    hideErrors(box);
    unblock(box);
  }

  document.addEventListener("htmx:beforeRequest", function (event) {
    var box = area();
    var link = linkOf(event);
    if (!box || !link) return;
    lastLink = link;
    box.setAttribute("aria-busy", "true");
    box.removeAttribute("data-day-failed");
    hideErrors(box);
    var name = box.querySelector("[data-day-name]");
    var label = link.getAttribute("data-day-label");
    if (name && label) name.textContent = label;
    block(box);
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    var box = area();
    if (!box || event.target !== box) return;
    retrying = false;
    clear(box);
  });

  function failed(kind) {
    return function (event) {
      var box = area();
      if (!box || !linkOf(event)) return;
      box.setAttribute("aria-busy", "false");
      box.setAttribute("data-day-failed", kind);
      var element = showError(box, kind === "slow" ? "slow" : "failed");
      var again = element && element.querySelector("[data-day-retry]");
      if (retrying && again) again.focus();
      retrying = false;
    };
  }

  document.addEventListener("htmx:timeout", failed("slow"));
  document.addEventListener("htmx:responseError", failed("failed"));
  document.addEventListener("htmx:sendError", failed("failed"));

  document.addEventListener("click", function (event) {
    var origin = event.target;
    var button = origin && origin.closest ? origin.closest("[data-day-retry]") : null;
    if (!button || !window.htmx) return;
    if (!lastLink || !lastLink.isConnected) return;
    retrying = true;
    window.htmx.trigger(lastLink, "click");
  });

  // Capture: while the area loads or has failed, its form is not sent
  document.addEventListener(
    "submit",
    function (event) {
      var box = area();
      var form = event.target;
      if (!box || !form || !form.closest || !form.closest("#" + AREA_ID)) return;
      if (!isLoading(box) && !isFailed(box)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    },
    true
  );

  function reset() {
    var box = area();
    if (box) clear(box);
    lastLink = null;
    retrying = false;
  }

  window.addEventListener("pageshow", reset);
  document.addEventListener("htmx:historyRestore", reset);
})();
