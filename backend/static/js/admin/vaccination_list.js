/* ============================================================
   vaccination_list.js
   For: backend/templates/admin/medical/vaccination_list.html
   Each row's action links to that PET's full vaccination history
   (not a single vaccination detail page), so the panel shows
   vaccination_history.html content — same "grab all children of
   .admin-page" technique as owner_detail.js / pet_detail.js.
   ============================================================ */

document.addEventListener('DOMContentLoaded', function () {
  var tableWrapper = document.querySelector('.admin-table-wrapper');
  if (!tableWrapper) return; // empty state — nothing to build a panel for

  // Guard: don't rebuild if this has already run once on this page
  if (document.querySelector('.vaccinations-layout')) return;

  buildLayout(tableWrapper);
  setupDynamicHeight();

  function buildLayout(tableWrapper) {
    var layout = document.createElement('div');
    layout.className = 'vaccinations-layout';

    var listColumn = document.createElement('div');
    listColumn.className = 'vaccinations-list-column';

    var panel = document.createElement('aside');
    panel.className = 'vaccination-detail-panel';
    panel.id = 'vaccination-detail-panel';
    panel.innerHTML = buildEmptyPanel();

    var parent = tableWrapper.parentNode;
    parent.insertBefore(layout, tableWrapper);
    listColumn.appendChild(tableWrapper);
    layout.appendChild(listColumn);
    layout.appendChild(panel);

    listColumn.addEventListener('click', function (e) {
      // Let any explicit link (Pet, Owner, or View All for Pet) navigate normally
      if (e.target.closest('a')) return;

      var row = e.target.closest('.admin-table-row');
      if (!row) return;

      var viewLink = row.querySelector('a.btn-small-primary');
      if (!viewLink) return;

      selectRow(listColumn, row);
      loadDetailIntoPanel(panel, viewLink.getAttribute('href'));
    });
  }

  function buildEmptyPanel() {
    return (
      '<div class="vaccination-detail-empty">' +
      '<p class="vaccination-detail-empty-text">Click a vaccination to view that pet\'s full history here.</p>' +
      '</div>'
    );
  }

  function selectRow(listColumn, row) {
    var allRows = listColumn.querySelectorAll('.admin-table-row');
    allRows.forEach(function (r) { r.classList.remove('admin-table-row--selected'); });
    row.classList.add('admin-table-row--selected');
  }

  /* Fetches the real vaccination_history.html page and moves its content
     into the panel — grabs every child of the fetched .admin-page (this
     template has no single wrapping content class), same technique as
     owner_detail.js / pet_detail.js. */
  function loadDetailIntoPanel(panel, url) {
    if (!url || url === '#') return;

    panel.innerHTML =
      '<h2 class="vaccination-detail-title">Vaccination History</h2>' +
      '<p class="vaccination-detail-loading">Loading…</p>';

    fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (response) {
        if (!response.ok) throw new Error('Request failed');
        return response.text();
      })
      .then(function (html) {
        var parser = new DOMParser();
        var doc = parser.parseFromString(html, 'text/html');
        var sourcePage = doc.querySelector('.admin-page');

        if (!sourcePage || !sourcePage.children.length) {
          panel.innerHTML =
            '<h2 class="vaccination-detail-title">Vaccination History</h2>' +
            '<p class="vaccination-detail-empty-text">Unable to load this pet\'s history.</p>';
          return;
        }

        panel.innerHTML = '';

        while (sourcePage.firstChild) {
          panel.appendChild(sourcePage.firstChild);
        }

        if (window.htmx) {
          window.htmx.process(panel);
        }
      })
      .catch(function () {
        panel.innerHTML =
          '<h2 class="vaccination-detail-title">Vaccination History</h2>' +
          '<p class="vaccination-detail-empty-text">Failed to load. Try again.</p>';
      });
  }

  /* ---- Dynamic height — measures actual position, doesn't guess pixels ---- */
  function setupDynamicHeight() {
    var listColumn = document.querySelector('.vaccinations-list-column');
    var panelEl = document.querySelector('.vaccination-detail-panel');
    var bottomMargin = 24;

    function applyHeights() {
      [listColumn, panelEl].forEach(function (el) {
        if (!el) return;
        var top = el.getBoundingClientRect().top;
        var available = window.innerHeight - top - bottomMargin;
        el.style.maxHeight = Math.max(available, 200) + 'px';
      });
    }

    applyHeights();
    window.addEventListener('resize', applyHeights);
  }
});