document.addEventListener('DOMContentLoaded', function () {
  var recordList = document.querySelector('.record-list');
  if (!recordList) return; // empty state — nothing to build a panel for

  // Guard: don't rebuild if this has already run once on this page
  if (document.querySelector('.history-layout')) return;

  buildLayout(recordList);
  setupDynamicHeight();

  function buildLayout(recordList) {
    var layout = document.createElement('div');
    layout.className = 'history-layout';

    var listColumn = document.createElement('div');
    listColumn.className = 'history-list-column';

    var panel = document.createElement('aside');
    panel.className = 'record-detail-panel';
    panel.id = 'record-detail-panel';
    panel.innerHTML = buildEmptyPanel();

    var parent = recordList.parentNode;
    parent.insertBefore(layout, recordList);
    listColumn.appendChild(recordList);
    layout.appendChild(listColumn);
    layout.appendChild(panel);

    listColumn.addEventListener('click', function (e) {
      // Let the "View" link navigate normally
      if (e.target.closest('a')) return;

      var card = e.target.closest('.record-card');
      if (!card) return;

      var viewLink = card.querySelector('a.btn-link');
      if (!viewLink) return;

      selectCard(listColumn, card);
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

  function selectCard(listColumn, card) {
    var allCards = listColumn.querySelectorAll('.record-card');
    allCards.forEach(function (c) { c.classList.remove('record-card--selected'); });
    card.classList.add('record-card--selected');
  }

  /* Fetches the real record_detail.html page and moves its .detail-layout
     into the panel — same technique as appointments_list.js, since this
     template also wraps everything in one .detail-layout container. */
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
    var listColumn = document.querySelector('.history-list-column');
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