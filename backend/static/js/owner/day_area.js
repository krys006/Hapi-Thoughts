/*
  Swap areas of the owner pages (Slice 8b-10b-3, generalised in 8b-10b-4).
  Loaded by layouts/owner.html. Plain browser JavaScript, no libraries. Works on
  every element marked data-swap-area: the free-times area (#day-area, booking
  step 2 and Change day or time) and the search results of My Pets, Bills and
  the Health library. HTMX swaps only that area's content; the trigger is a link
  (day, month, chip) or a form (the search). Every visible word lives in the
  templates (owner/swap_parts/states.html and the pages): this file has no English
  text. It does nothing on a page without a swap area. File name kept from 10b-3.

  Why one file: the timeout, failure and retry logic is the same for all four
  areas, so it exists once. The area is found from the request's target
  (event.detail.target), not from the trigger, because a search form sits outside
  the area it fills.

  - A request starts (htmx:beforeRequest): the area is aria-busy, the old boxes
    are hidden, and the element that started it is remembered for the retry button. The
    loading look (grey boxes, the search spinner and its status line) is CSS only, while
    htmx has the htmx-request class on the area or on the search form.
  - It works (htmx:afterSwap): the area has fresh content, so every state is gone.
  - It fails (htmx:timeout after 10 s, htmx:responseError, htmx:sendError, also
    offline): nothing was swapped. data-swap-failed on the area keeps the grey
    boxes (dimmed) and hides the old results; the matching [data-swap-error] box
    shows.
  - [data-swap-retry] sends the same request again: a link is clicked again by
    HTMX; a form is submitted again through HTMX (the form's hx-trigger has
    submit), so its typed text and chosen options go with it.
  - pageshow and htmx:historyRestore clear everything (a page back from the
    back/forward cache is never left loading).

  Only the free-times area (#day-area) also does these, because only it has a
  Next / Send button and day names: the day's name (data-day-label of the tapped
  link) is written into [data-day-name] with textContent, the form's primary button
  is blocked (aria-disabled plus the muted look of [data-busy-muted]) while it loads
  or after a failure (the old times are hidden), and a capture submit guard stops
  the area's form then, so a stale time can never be sent. The Search buttons of
  Bills and the Health library are never blocked.
*/
(function () {
  "use strict";

  var AREA = "[data-swap-area]";
  var DAY_AREA_ID = "day-area";
  var starters = new WeakMap(); // area -> the element that started its request
  var retrying = new WeakSet(); // areas whose request is a retry

  function areaOf(event) {
    var detail = event.detail || {};
    var target = detail.target || event.target;
    return target && target.closest ? target.closest(AREA) : null;
  }

  function starterOf(event) {
    var detail = event.detail || {};
    return detail.elt || event.target;
  }

  function isDayArea(box) {
    return box.id === DAY_AREA_ID;
  }

  function isLoading(box) {
    return box.classList.contains("htmx-request");
  }

  function isFailed(box) {
    return box.hasAttribute("data-swap-failed");
  }

  // The day area's form has one violet button (Next / Send new time) outside it
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
    var button = isDayArea(box) ? primaryButton(box) : null;
    if (!button) return;
    button.setAttribute("aria-disabled", "true");
    button.setAttribute("data-busy-muted", "");
    button.setAttribute("data-day-blocked", "");
  }

  function unblock(box) {
    var button = isDayArea(box) ? primaryButton(box) : null;
    if (!button || !button.hasAttribute("data-day-blocked")) return;
    button.removeAttribute("data-day-blocked");
    button.removeAttribute("data-busy-muted");
    button.removeAttribute("aria-disabled");
  }

  function hideErrors(box) {
    Array.prototype.forEach.call(
      box.querySelectorAll("[data-swap-error]"),
      function (element) {
        element.hidden = true;
      }
    );
  }

  function showError(box, kind) {
    hideErrors(box);
    var element = box.querySelector('[data-swap-error="' + kind + '"]');
    if (!element) return null;
    element.hidden = false;
    return element;
  }

  // Back to the quiet state: nothing loading, nothing failed
  function clear(box) {
    box.setAttribute("aria-busy", "false");
    box.removeAttribute("data-swap-failed");
    box.classList.remove("htmx-request");
    hideErrors(box);
    unblock(box);
  }

  // The free-times area names the day or month that is loading
  function nameTheDay(box, starter) {
    if (!isDayArea(box) || !starter || !starter.getAttribute) return;
    var name = box.querySelector("[data-day-name]");
    var label = starter.getAttribute("data-day-label");
    if (name && label) name.textContent = label;
  }

  document.addEventListener("htmx:beforeRequest", function (event) {
    var box = areaOf(event);
    if (!box) return;
    var starter = starterOf(event);
    starters.set(box, starter);
    box.setAttribute("aria-busy", "true");
    box.removeAttribute("data-swap-failed");
    hideErrors(box);
    nameTheDay(box, starter);
    block(box);
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    var box = event.target;
    if (!box || !box.hasAttribute || !box.hasAttribute("data-swap-area")) return;
    retrying.delete(box);
    clear(box);
  });

  function failed(kind) {
    return function (event) {
      var box = areaOf(event);
      if (!box) return;
      box.setAttribute("aria-busy", "false");
      box.setAttribute("data-swap-failed", kind);
      var element = showError(box, kind === "slow" ? "slow" : "failed");
      var again = element && element.querySelector("[data-swap-retry]");
      if (retrying.has(box) && again) again.focus();
      retrying.delete(box);
    };
  }

  document.addEventListener("htmx:timeout", failed("slow"));
  document.addEventListener("htmx:responseError", failed("failed"));
  document.addEventListener("htmx:sendError", failed("failed"));

  // Retry button: the same request is sent through HTMX again
  document.addEventListener("click", function (event) {
    var origin = event.target;
    var button = origin && origin.closest ? origin.closest("[data-swap-retry]") : null;
    if (!button || !window.htmx) return;
    var box = button.closest(AREA);
    var starter = box ? starters.get(box) : null;
    if (!starter || !starter.isConnected) return;
    retrying.add(box);
    window.htmx.trigger(starter, starter.tagName === "FORM" ? "submit" : "click");
  });

  // Capture: while the free-times area loads or has failed, its form is not sent
  document.addEventListener(
    "submit",
    function (event) {
      var form = event.target;
      var box = form && form.closest ? form.closest("#" + DAY_AREA_ID) : null;
      if (!box) return;
      if (!isLoading(box) && !isFailed(box)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    },
    true
  );

  function reset() {
    Array.prototype.forEach.call(document.querySelectorAll(AREA), clear);
    // A search form restored while it was marked busy (hx-indicator)
    Array.prototype.forEach.call(
      document.querySelectorAll("form[hx-indicator]"),
      function (form) {
        form.classList.remove("htmx-request");
      }
    );
  }

  window.addEventListener("pageshow", reset);
  document.addEventListener("htmx:historyRestore", reset);
})();
