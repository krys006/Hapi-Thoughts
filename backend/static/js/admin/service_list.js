document.addEventListener('DOMContentLoaded', function () {
  setupAddServiceModal();

  var table = document.querySelector('.data-table');
  if (!table) return; // empty state — nothing to build a panel for

  // Guard: don't rebuild if this has already run once on this page
  if (document.querySelector('.services-layout')) return;

  buildLayout(table);
  setupDynamicHeight();

  function buildLayout(table) {
    var layout = document.createElement('div');
    layout.className = 'services-layout';

    var listColumn = document.createElement('div');
    listColumn.className = 'services-list-column';

    var panel = document.createElement('aside');
    panel.className = 'service-detail-panel';
    panel.id = 'service-detail-panel';
    panel.innerHTML = buildEmptyPanel();

    var parent = table.parentNode;
    parent.insertBefore(layout, table);
    listColumn.appendChild(table);
    layout.appendChild(listColumn);
    layout.appendChild(panel);

    listColumn.addEventListener('click', function (e) {
      // Let the Edit link navigate normally
      if (e.target.closest('a')) return;

      var row = e.target.closest('tbody tr');
      if (!row) return;

      var editLink = row.querySelector('a.btn-link');
      if (!editLink) return;

      selectRow(listColumn, row);
      loadEditFormIntoPanel(panel, editLink.getAttribute('href'));
    });
  }

  function buildEmptyPanel() {
    return (
      '<div class="service-detail-empty">' +
      '<p class="service-detail-empty-text">Click a service to edit it here.</p>' +
      '</div>'
    );
  }

  function selectRow(listColumn, row) {
    var allRows = listColumn.querySelectorAll('tbody tr');
    allRows.forEach(function (r) { r.classList.remove('data-table-row--selected'); });
    row.classList.add('data-table-row--selected');
  }

  function resetPanel(panel) {
    panel.innerHTML = buildEmptyPanel();
  }

  /* Fetches the real service_form.html page and moves its form into
     the panel. */
  function loadEditFormIntoPanel(panel, url) {
    if (!url || url === '#') return;

    panel.innerHTML =
      '<h2 class="service-detail-title">Edit Service</h2>' +
      '<p class="service-detail-loading">Loading…</p>';

    fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (response) {
        if (!response.ok) throw new Error('Request failed');
        return response.text();
      })
      .then(function (html) {
        var parser = new DOMParser();
        var doc = parser.parseFromString(html, 'text/html');
        var form = doc.querySelector('form.billing-form');

        if (!form) {
          panel.innerHTML =
            '<h2 class="service-detail-title">Edit Service</h2>' +
            '<p class="service-detail-empty-text">Unable to load this service.</p>';
          return;
        }

        // Same missing-action bug hit on several other forms today.
        if (!form.getAttribute('action')) {
          form.setAttribute('action', url);
        }

        panel.innerHTML = '<h2 class="service-detail-title">Edit Service</h2>';
        panel.appendChild(form);

        // Cancel normally navigates back to the service list — since
        // we're already on the list page, clear the panel instead.
        var cancelLink = form.querySelector('.form-actions a.btn-secondary');
        if (cancelLink) {
          cancelLink.addEventListener('click', function (e) {
            e.preventDefault();
            var allRows = document.querySelectorAll('tbody tr');
            allRows.forEach(function (r) { r.classList.remove('data-table-row--selected'); });
            resetPanel(panel);
          });
        }

        if (window.htmx) {
          window.htmx.process(form);
        }
      })
      .catch(function () {
        panel.innerHTML =
          '<h2 class="service-detail-title">Edit Service</h2>' +
          '<p class="service-detail-empty-text">Failed to load service. Try again.</p>';
      });
  }

  /* ---- Dynamic height — measures actual position, doesn't guess pixels ---- */
  function setupDynamicHeight() {
    var listColumn = document.querySelector('.services-list-column');
    var panelEl = document.querySelector('.service-detail-panel');
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

  /* ---- Add Service modal ----
     "+ Add Service" always exists (even on the empty state), so this
     binds directly rather than using event delegation. */
  function setupAddServiceModal() {
    var trigger = document.querySelector('.admin-page-header a.btn-primary');
    if (!trigger) return;

    var formUrl = trigger.getAttribute('href');

    trigger.addEventListener('click', function (e) {
      e.preventDefault();
      openAddServiceModal(formUrl);
    });
  }

  function openAddServiceModal(url) {
    var overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'add-service-modal-overlay';

    var box = document.createElement('div');
    box.className = 'modal-box';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');

    box.innerHTML =
      '<div class="modal-header">' +
      '<h2 class="modal-title">Add Service</h2>' +
      '<button type="button" class="modal-close" aria-label="Close">&times;</button>' +
      '</div>' +
      '<div class="modal-body">' +
      '<p class="service-detail-loading">Loading…</p>' +
      '</div>';

    overlay.appendChild(box);
    document.body.appendChild(overlay);
    document.body.classList.add('modal-open');

    function closeModal() {
      overlay.remove();
      document.body.classList.remove('modal-open');
      document.removeEventListener('keydown', onKeydown);
    }

    function onKeydown(e) {
      if (e.key === 'Escape') closeModal();
    }

    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) closeModal();
    });

    box.querySelector('.modal-close').addEventListener('click', closeModal);
    document.addEventListener('keydown', onKeydown);

    var body = box.querySelector('.modal-body');

    fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (response) {
        if (!response.ok) throw new Error('Request failed');
        return response.text();
      })
      .then(function (html) {
        var parser = new DOMParser();
        var doc = parser.parseFromString(html, 'text/html');
        var form = doc.querySelector('form.billing-form');

        if (!form) {
          body.innerHTML = '<p class="service-detail-empty-text">Unable to load the form.</p>';
          return;
        }

        if (!form.getAttribute('action')) {
          form.setAttribute('action', url);
        }

        body.innerHTML = '';
        body.appendChild(form);

        var cancelLink = form.querySelector('.form-actions a.btn-secondary');
        if (cancelLink) {
          cancelLink.addEventListener('click', function (e) {
            e.preventDefault();
            closeModal();
          });
        }

        if (window.htmx) {
          window.htmx.process(form);
        }
      })
      .catch(function () {
        body.innerHTML = '<p class="service-detail-empty-text">Failed to load the form. Try again.</p>';
      });
  }
});