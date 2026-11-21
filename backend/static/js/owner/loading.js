/*
  Loading states of the owner pages (Slice 8b-10b-1), loaded by layouts/owner.html
  and layouts/owner_auth.html. Plain browser JavaScript, no libraries.
  Markup and every visible word live in layouts/owner_parts/loading.html
  (cloned or read from there): this file has no English text.

  SENDING   a POST form (not data-loading="off", no hx- attribute): a second
            submit does nothing; the tapped button gets aria-busy, aria-disabled,
            data-busy, its data-busy-label and a spinner. The real disabled
            attribute is never set: a disabled button is not sent with the form,
            and several views read the button's name and value.
  PAGE OPEN a link or a GET form that opens another page: after 300 ms, if the
            page is still here, the top bar, the pill note, the dimmed <main>
            and a spinner on the tapped bottom tab ([data-tab-link]).
  SLOW      one timer (data-slow-after on #loading-root, 10000 ms). The retry
            button opens the page again, or stops a sending form and gives it back.
  OFFLINE   the strip shows while navigator.onLine is false; forms and links
            are held (nothing is marked as sending).
  RESET     pageshow (also from the back/forward cache) clears everything.
  One listener per event on document, so dialogs and swapped-in content work.
  Every element is optional: a missing one is skipped.
*/
(function () {
  "use strict";

  var root = document.getElementById("loading-root");
  if (!root) return;

  var data = root.dataset;
  var status = document.getElementById("loading-status");
  var bar = document.getElementById("loading-bar");
  var pill = document.getElementById("loading-pill");
  var pillLabel = pill && pill.querySelector("[data-loading-label]");
  var slow = document.getElementById("loading-slow");
  var offline = document.getElementById("loading-offline");
  var hold = offline && offline.querySelector("[data-loading-hold]");
  var template = document.getElementById("loading-spinner");

  var OPEN_DELAY = 300;
  var slowAfter = parseInt(data.slowAfter, 10) || 10000;

  var labels = new WeakMap(); // button -> its own child nodes while it is busy
  var openTimer = null;
  var slowTimer = null;
  var current = null; // what the retry button repeats: {kind, url} or {kind, button}
  var tab = null; // {icon, copy} while a bottom tab shows the spinner

  function announce(text) {
    if (status) status.textContent = text || "";
  }

  function setShown(element, shown) {
    if (element) element.hidden = !shown;
  }

  function wordsOf(element) {
    return element ? element.textContent.replace(/\s+/g, " ").trim() : "";
  }

  function spinner() {
    var first = template && template.content && template.content.firstElementChild;
    return first ? first.cloneNode(true) : null;
  }

  function isOff(element) {
    return !!element.closest('[data-loading="off"]');
  }

  function hasHtmx(form) {
    return Array.prototype.some.call(form.attributes, function (attribute) {
      return attribute.name.indexOf("hx-") === 0;
    });
  }

  // A form this script looks after, or null
  function managed(target) {
    if (!target || target.tagName !== "FORM") return null;
    if (isOff(target) || hasHtmx(target)) return null;
    var method = (target.getAttribute("method") || "get").toLowerCase();
    return method === "post" || method === "get" ? target : null;
  }

  function isPost(form) {
    return (form.getAttribute("method") || "").toLowerCase() === "post";
  }

  function submitButtons(form) {
    return Array.prototype.filter.call(form.elements, function (element) {
      return element.matches(
        'button[type="submit"], button:not([type]), input[type="submit"]'
      );
    });
  }

  // ---------- Sending (POST forms) ----------

  function makeBusy(button) {
    var text = button.getAttribute("data-busy-label") || data.busyDefault || "";
    button.setAttribute("aria-busy", "true");
    button.setAttribute("aria-disabled", "true");
    button.setAttribute("data-busy", "");
    if (button.tagName === "BUTTON" && !labels.has(button)) {
      labels.set(button, Array.prototype.slice.call(button.childNodes));
      var parts = [];
      var copy = spinner();
      if (copy) parts.push(copy);
      parts.push(document.createTextNode(text));
      button.replaceChildren.apply(button, parts);
    }
    return text;
  }

  function markSending(form, submitter) {
    var buttons = submitButtons(form);
    var button = submitter || buttons[0] || null;
    form.setAttribute("data-sending", "");
    buttons.forEach(function (other) {
      if (other === button) return;
      other.setAttribute("aria-disabled", "true");
      other.setAttribute("data-busy-muted", "");
    });
    var text = button ? makeBusy(button) : data.busyDefault;
    announce(text);
    current = { kind: "send", button: button };
    startSlow();
  }

  function clearSending() {
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-sending]"),
      function (form) {
        form.removeAttribute("data-sending");
      }
    );
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-busy], [data-busy-muted]"),
      function (button) {
        var nodes = labels.get(button);
        if (nodes) {
          button.replaceChildren.apply(button, nodes);
          labels.delete(button);
        }
        ["data-busy", "data-busy-muted", "aria-busy", "aria-disabled"].forEach(
          function (name) {
            button.removeAttribute(name);
          }
        );
      }
    );
  }

  // ---------- Page open (links and GET forms) ----------

  // The address a click will open, or "" when this script stays out of it
  function pageUrl(link, event) {
    if (event.button !== 0) return "";
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return "";
    var target = link.getAttribute("target");
    if (target && target !== "_self") return "";
    if (link.hasAttribute("download")) return "";
    if (link.hasAttribute("data-dialog-open")) return "";
    if (link.hasAttribute("data-flash-close")) return "";
    if (isOff(link)) return "";
    var href = link.getAttribute("href");
    if (!href || href.charAt(0) === "#") return "";
    var url;
    try {
      url = new URL(link.href, window.location.href);
    } catch (problem) {
      return "";
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return "";
    if (url.origin !== window.location.origin) return "";
    var here = url.pathname === window.location.pathname;
    if (here && url.search === window.location.search && url.hash) return "";
    return url.href;
  }

  function formUrl(form) {
    var url = new URL(form.getAttribute("action") || "", window.location.href);
    url.search = new URLSearchParams(new FormData(form)).toString();
    return url.href;
  }

  function openingText(name) {
    if (!name || !data.openingFormat) return data.openingDefault || "";
    return data.openingFormat.replace("{name}", name);
  }

  function spinTab(link) {
    var icon = link.querySelector("svg");
    var copy = spinner();
    if (!icon || !copy) return;
    copy.classList.replace("size-5", "size-6");
    icon.replaceWith(copy);
    tab = { icon: icon, copy: copy };
  }

  function restoreTab() {
    if (tab && tab.copy.parentNode) tab.copy.replaceWith(tab.icon);
    tab = null;
  }

  function reveal(url, name, link) {
    current = { kind: "open", url: url };
    setShown(bar, true);
    var main = document.querySelector("main");
    if (main) main.setAttribute("aria-busy", "true");
    var text = openingText(name);
    if (pillLabel) pillLabel.textContent = text;
    setShown(pill, true);
    if (link) spinTab(link);
    announce(text);
    startSlow();
  }

  // Wait a moment first: a quick page never flashes the bar
  function queueOpen(event, url, name, link) {
    window.clearTimeout(openTimer);
    openTimer = window.setTimeout(function () {
      openTimer = null;
      if (event.defaultPrevented) return;
      reveal(url, name, link);
    }, OPEN_DELAY);
  }

  // ---------- Slow ----------

  function startSlow() {
    window.clearTimeout(slowTimer);
    slowTimer = window.setTimeout(function () {
      slowTimer = null;
      setShown(pill, false);
      setShown(slow, true);
      announce(wordsOf(slow && slow.querySelector("[data-loading-slow-text]")));
    }, slowAfter);
  }

  function resetAll() {
    window.clearTimeout(openTimer);
    window.clearTimeout(slowTimer);
    openTimer = null;
    slowTimer = null;
    clearSending();
    restoreTab();
    setShown(bar, false);
    setShown(pill, false);
    setShown(slow, false);
    var main = document.querySelector("main");
    if (main) main.removeAttribute("aria-busy");
    current = null;
  }

  // ---------- Offline ----------

  function showHold() {
    var text = data.offlineHold || "";
    if (hold) {
      hold.textContent = text;
      hold.hidden = false;
    }
    announce(text);
  }

  function clearHold() {
    if (!hold) return;
    hold.textContent = "";
    hold.hidden = true;
  }

  function syncOffline(quiet) {
    var down = !navigator.onLine;
    setShown(offline, down);
    if (down) {
      announce(wordsOf(offline));
    } else {
      clearHold();
      if (!quiet) announce(data.onlineMessage || "");
    }
  }

  // ---------- Listeners ----------

  // Capture: a second submit does nothing; offline holds every form
  document.addEventListener(
    "submit",
    function (event) {
      var form = managed(event.target);
      if (!form) return;
      if (!navigator.onLine) {
        event.preventDefault();
        showHold();
        return;
      }
      if (isPost(form)) {
        if (form.hasAttribute("data-sending")) event.preventDefault();
      } else {
        queueOpen(event, formUrl(form), "", null);
      }
    },
    true
  );

  // Bubble: only a submit nobody stopped marks the form as sending
  document.addEventListener("submit", function (event) {
    var form = managed(event.target);
    if (!form || !isPost(form) || event.defaultPrevented) return;
    markSending(form, event.submitter);
  });

  document.addEventListener(
    "click",
    function (event) {
      var origin = event.target;
      var link = origin && origin.closest ? origin.closest("a[href]") : null;
      if (!link) return;
      var url = pageUrl(link, event);
      if (!url) return;
      if (!navigator.onLine) {
        event.preventDefault();
        showHold();
        return;
      }
      var name = link.getAttribute("data-open-label");
      queueOpen(event, url, name, link.matches("[data-tab-link]") ? link : null);
    },
    true
  );

  document.addEventListener("click", function (event) {
    if (!event.target.closest("[data-loading-retry]")) return;
    var job = current;
    if (job && job.kind === "open") {
      window.location.assign(job.url);
      return;
    }
    // A sending form: stop the request, give the form back, focus its button
    window.stop();
    var button = job && job.button;
    resetAll();
    if (button && button.isConnected) button.focus();
  });

  window.addEventListener("pageshow", function () {
    resetAll();
    syncOffline(true);
  });
  window.addEventListener("pagehide", function () {
    window.clearTimeout(openTimer);
    window.clearTimeout(slowTimer);
  });
  window.addEventListener("offline", function () {
    syncOffline(false);
  });
  window.addEventListener("online", function () {
    syncOffline(false);
  });

  syncOffline(true);
})();
