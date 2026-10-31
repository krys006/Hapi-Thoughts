/* ============================================================
   header_notifications.js
   For: backend/templates/base_dashboard.html
   Shared across every page (bell button lives in the common
   header). Adds real open/close toggle behavior — by default,
   HTMX just re-fetches and replaces the panel content on every
   click, so clicking twice reloads it rather than closing it.
   ============================================================ */

document.addEventListener('DOMContentLoaded', function () {
  var bellButton = document.getElementById('bell-button');
  var panelContainer = document.getElementById('notification-panel-container');
  if (!bellButton || !panelContainer) return;

  // Move the panel to be a direct child of <body>. Even with
  // position:fixed, an element stays trapped inside any ANCESTOR's
  // stacking context if one exists — and the header does (position:
  // sticky + z-index together create one). That means the panel's own
  // z-index only ever gets compared within the header's context, not
  // globally against the rest of the page, which is why it could still
  // end up rendering behind .dashboard-main despite a "higher" z-index.
  // Moving it to <body> directly sidesteps this completely — the exact
  // same technique already working for every modal built today, all of
  // which are created via document.body.appendChild(...) for this
  // reason. The element keeps its id, so HTMX's hx-target="#notification-
  // panel-container" still finds and fills it normally regardless of
  // where it now lives in the DOM.
  document.body.appendChild(panelContainer);

  // Before HTMX fires its request: if the panel already has content
  // (i.e. it's currently open), cancel the fetch and just close it
  // instead. State is derived from the actual DOM content each time,
  // not a separate tracked variable, so it can't drift out of sync.
  bellButton.addEventListener('htmx:beforeRequest', function (e) {
    var isOpen = panelContainer.innerHTML.trim() !== '';
    if (isOpen) {
      e.preventDefault();
      panelContainer.innerHTML = '';
      bellButton.classList.remove('bell-button--active');
    }
  });

  bellButton.addEventListener('htmx:afterRequest', function () {
    bellButton.classList.add('bell-button--active');
  });

  // Click anywhere outside the button or panel closes it too —
  // standard dropdown behavior.
  document.addEventListener('click', function (e) {
    var isOpen = panelContainer.innerHTML.trim() !== '';
    if (!isOpen) return;

    var clickedInside = bellButton.contains(e.target) || panelContainer.contains(e.target);
    if (!clickedInside) {
      panelContainer.innerHTML = '';
      bellButton.classList.remove('bell-button--active');
    }
  });

  // Escape key closes it too
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var isOpen = panelContainer.innerHTML.trim() !== '';
    if (isOpen) {
      panelContainer.innerHTML = '';
      bellButton.classList.remove('bell-button--active');
    }
  });
});