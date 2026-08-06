document.addEventListener('DOMContentLoaded', function () {
  var tableWrapper = document.querySelector('.admin-table-wrapper');
  if (!tableWrapper) return; // empty state — nothing to build a panel for

  // Guard: don't rebuild if this has already run once on this page
  if (document.querySelector('.records-layout')) return;

  buildLayout(tableWrapper);
  setupDynamicHeight();

  function buildLayout(tableWrapper) {
    var layout = document.createElement('div');
    layout.className = 'records-layout';

    var listColumn = document.createElement('div');
    listColumn.className = 'records-list-column';

    var panel = document.createElement('aside');
    panel.className = 'record-detail-panel';
    panel.id = 'record-detail-panel';
    panel.innerHTML = buildEmptyPanel();

    var parent = tableWrapper.parentNode;
    parent.insertBefore(layout, tableWrapper);
    listColumn.appendChild(tableWrapper);
    layout.appendChild(listColumn);
    layout.appendChild(panel);

    listColumn.addEventListener('click', function (e) {
      // Let any explicit link (Pet, Owner, or View) navigate normally
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
      '<div class="record-detail-empty">' +
      '<p class="record-detail-empty-text">Click a record to view its full details here.</p>' +
      '</div>'
    );
  }

  function selectRow(listColumn, row) {
    var allRows = listColumn.querySelectorAll('.admin-table-row');
    allRows.forEach(function (r) { r.classList.remove('admin-table-row--selected'); });
    row.classList.add('admin-table-row--selected');
  }

  /* Fetches the real record_detail.html page and moves its .detail-layout
     into the panel — same technique as pet_history.js. */
  function loadDetailIntoPanel(panel, url) {
    if (!url || url === '#') return;

    panel.innerHTML =
      '<h2 class="record-detail-title">Medical Record</h2>' +
      '<p class="record-detail-loading">Loading…</p>';

    fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (response) {
        if (!response.ok) throw new Error('Request failed');
        return response.text();
      })
      .then(function (html) {
        var parser = new DOMParser();
        var doc = parser.parseFromString(html, 'text/html');
        var layout = doc.querySelector('.detail-layout');

        if (!layout) {
          panel.innerHTML =
            '<h2 class="record-detail-title">Medical Record</h2>' +
            '<p class="record-detail-empty-text">Unable to load this record.</p>';
          return;
        }

        panel.innerHTML = '<h2 class="record-detail-title">Medical Record</h2>';
        panel.appendChild(layout);

        if (window.htmx) {
          window.htmx.process(layout);
        }
      })
      .catch(function () {
        panel.innerHTML =
          '<h2 class="record-detail-title">Medical Record</h2>' +
          '<p class="record-detail-empty-text">Failed to load record. Try again.</p>';
      });
  }

  /* ---- Dynamic height — measures actual position, doesn't guess pixels ---- */
  function setupDynamicHeight() {
    var listColumn = document.querySelector('.records-list-column');
    var panelEl = document.querySelector('.record-detail-panel');
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