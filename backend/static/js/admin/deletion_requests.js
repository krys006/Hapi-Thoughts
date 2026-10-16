document.addEventListener('DOMContentLoaded', function () {
  repairBrokenLinks();

  var sections = document.querySelectorAll('.admin-detail-section');
  if (!sections.length) return;

  // Guard: don't rebuild if this has already run once on this page
  if (document.querySelector('.deletion-layout')) return;

  buildLayout(sections);
  setupDynamicHeight();

  /* ---- Step 1: repair broken links in the Pending table ---- */
  function repairBrokenLinks() {
    var pendingRows = document.querySelectorAll('.admin-table-row');

    pendingRows.forEach(function (row) {
      var cells = row.querySelectorAll('td.admin-table-cell');
      if (cells.length < 2) return;

      repairCell(cells[0]); // Pet
      repairCell(cells[1]); // Owner
    });
  }

  function repairCell(cell) {
    var raw = cell.textContent;
    var hrefMatch = raw.match(/href="([^"]+)"/);
    if (!hrefMatch) return; // not broken (e.g. Resolved table's plain text cells)

    var href = hrefMatch[1];
    var parts = raw.split('>');
    var label = parts[parts.length - 1].trim();

    cell.innerHTML = '';
    var link = document.createElement('a');
    link.setAttribute('href', href);
    link.className = 'admin-table-link';
    link.textContent = label;
    cell.appendChild(link);
  }

  /* ---- Step 2: build the two-column layout ---- */
  function buildLayout(sections) {
    var firstSection = sections[0];
    var parent = firstSection.parentNode;

    var layout = document.createElement('div');
    layout.className = 'deletion-layout';

    var listColumn = document.createElement('div');
    listColumn.className = 'deletion-list-column';

    var panel = document.createElement('aside');
    panel.className = 'pet-detail-panel'; // reuses pet_detail.css styling
    panel.id = 'deletion-detail-panel';
    panel.innerHTML = buildEmptyPanel();

    parent.insertBefore(layout, firstSection);
    sections.forEach(function (section) {
      listColumn.appendChild(section);
    });
    layout.appendChild(listColumn);
    layout.appendChild(panel);

    listColumn.addEventListener('click', function (e) {
      // Let any explicit link/button navigate or submit normally
      if (e.target.closest('a') || e.target.closest('form')) return;

      var row = e.target.closest('.admin-table-row');
      if (!row) return;

      // Pet link is always the first cell
      var petLink = row.querySelector('td:first-child a.admin-table-link');
      if (!petLink) return; // Resolved rows have no link — not clickable

      selectRow(listColumn, row);
      loadDetailIntoPanel(panel, petLink.getAttribute('href'));
    });
  }

  function buildEmptyPanel() {
    return (
      '<div class="pet-detail-empty">' +
      '<p class="pet-detail-empty-text">Click a pending request to view the pet\'s full details here.</p>' +
      '</div>'
    );
  }

  function selectRow(listColumn, row) {
    var allRows = listColumn.querySelectorAll('.admin-table-row');
    allRows.forEach(function (r) { r.classList.remove('admin-table-row--selected'); });
    row.classList.add('admin-table-row--selected');
  }

  /* Fetches the real pet_detail.html page and moves its content into the
     panel — identical technique to pets_list.js, reused here so a
     deletion request can be reviewed alongside the pet's full record. */
  function loadDetailIntoPanel(panel, url) {
    if (!url || url === '#') return;

    panel.innerHTML =
      '<h2 class="pet-detail-title">Pet Details</h2>' +
      '<p class="pet-detail-loading">Loading…</p>';

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
            '<h2 class="pet-detail-title">Pet Details</h2>' +
            '<p class="pet-detail-empty-text">Unable to load details for this pet.</p>';
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
          '<h2 class="pet-detail-title">Pet Details</h2>' +
          '<p class="pet-detail-empty-text">Failed to load pet details. Try again.</p>';
      });
  }

  /* ---- Dynamic height (measures actual position, doesn't guess pixels —
     same approach that worked correctly on pets_list.js) ---- */
  function setupDynamicHeight() {
    var listColumn = document.querySelector('.deletion-list-column');
    var panelEl = document.querySelector('.pet-detail-panel');
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