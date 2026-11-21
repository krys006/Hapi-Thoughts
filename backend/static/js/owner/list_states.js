/*
  List states of the owner forms (Slice 8b-10b-2): the Barangay list that loads
  after the Town, and the Breed list that loads after the Species (HTMX).
  Loaded by layouts/owner.html. Plain browser JavaScript, no libraries.
  Every visible word lives in owner/list_state_parts/list_status.html: this
  file has no English text. It only reacts to selects with [data-list-field].

  - While the request runs, the select that gets the list (the hx-target) is
    locked with the real disabled attribute, so a half-chosen value is never
    posted. The loading line is shown by htmx itself (hx-indicator).
  - A swap that arrives leaves the select as address_form.js / pet_form.js set
    it. A request that ends without a swap gives the select back as it was.
  - An error answer, a time-out or no connection (htmx:responseError,
    htmx:sendError, htmx:timeout): nothing was swapped, so the old options stay,
    the select stays locked, and the field's "couldn't load" line shows
    ([data-list-error] on the Town / Species select points at it).
  - [data-list-retry] in that line sends the same request again (change event
    on the Town / Species select). A good answer hides the line.
  The form can still be sent while a list is loading or has failed: the locked
  select is simply not posted, and the Province / Species select (with its
  typed boxes) is never locked.
  One listener per event on document. Every element is optional.
*/
(function () {
  "use strict";

  var FIELD = "[data-list-field]";
  var pending = new WeakMap(); // trigger select -> {target, retrying}

  function triggerOf(event) {
    var detail = event.detail || {};
    var element = detail.elt || event.target;
    return element && element.closest ? element.closest(FIELD) : null;
  }

  function failedLine(trigger) {
    var selector = trigger.getAttribute("data-list-error");
    if (!selector) return null;
    try {
      return document.querySelector(selector);
    } catch (problem) {
      return null;
    }
  }

  function showFailed(trigger, shown) {
    var line = failedLine(trigger);
    if (line) line.hidden = !shown;
    return line;
  }

  function forget(target) {
    ["data-list-locked", "data-list-swapped", "data-list-was-disabled"].forEach(
      function (name) {
        target.removeAttribute(name);
      }
    );
  }

  function lock(target) {
    if (!target || !target.hasAttribute) return;
    target.removeAttribute("data-list-broken");
    if (target.hasAttribute("data-list-locked")) return;
    var was = target.disabled ? "yes" : "no";
    target.setAttribute("data-list-was-disabled", was);
    target.setAttribute("data-list-locked", "");
    target.disabled = true;
  }

  // The request ended well: a swap already set the select; no swap: as before
  function release(target) {
    if (!target || !target.hasAttribute("data-list-locked")) return;
    if (!target.hasAttribute("data-list-swapped")) {
      target.disabled = target.getAttribute("data-list-was-disabled") === "yes";
    }
    forget(target);
  }

  document.addEventListener("htmx:beforeRequest", function (event) {
    var trigger = triggerOf(event);
    if (!trigger) return;
    var target = event.detail && event.detail.target;
    var before = pending.get(trigger);
    pending.set(trigger, { target: target, retrying: !!(before && before.retrying) });
    lock(target);
    showFailed(trigger, false);
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    var target = event.target;
    if (target && target.hasAttribute && target.hasAttribute("data-list-locked")) {
      target.setAttribute("data-list-swapped", "");
    }
  });

  document.addEventListener("htmx:afterRequest", function (event) {
    var trigger = triggerOf(event);
    if (!trigger || !event.detail || !event.detail.successful) return;
    var state = pending.get(trigger) || {};
    var target = state.target || event.detail.target;
    release(target);
    showFailed(trigger, false);
    if (state.retrying && target && !target.disabled) target.focus();
    pending.delete(trigger);
  });

  function failed(event) {
    var trigger = triggerOf(event);
    if (!trigger) return;
    var state = pending.get(trigger) || {};
    var target = state.target || (event.detail && event.detail.target);
    if (target && target.hasAttribute) {
      forget(target);
      target.setAttribute("data-list-broken", "");
      target.disabled = true;
    }
    var line = showFailed(trigger, true);
    var again = line && line.querySelector("[data-list-retry]");
    if (state.retrying && again) again.focus();
    pending.set(trigger, { target: target, retrying: false });
  }

  ["htmx:responseError", "htmx:sendError", "htmx:timeout"].forEach(function (name) {
    document.addEventListener(name, failed);
  });

  document.addEventListener("click", function (event) {
    var origin = event.target;
    var button = origin && origin.closest ? origin.closest("[data-list-retry]") : null;
    if (!button || !window.htmx) return;
    var line = button.closest("[data-list-failed]");
    if (!line || !line.id) return;
    var trigger = document.querySelector('[data-list-error="#' + line.id + '"]');
    if (!trigger) return;
    var state = pending.get(trigger) || {};
    pending.set(trigger, { target: state.target, retrying: true });
    window.htmx.trigger(trigger, "change");
  });
})();
