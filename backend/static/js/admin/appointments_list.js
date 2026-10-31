document.addEventListener('DOMContentLoaded', function () {
  var listContainer = document.getElementById('appointment-list');
  if (!listContainer) return;

  // Guard: don't rebuild if this has already run once on this page
  if (document.querySelector('.appointments-layout')) return;

  var table = listContainer.querySelector('.data-table');
  if (table) {
    reorderHeader(table);
    reorderRows(table);
  }

  buildLayout(listContainer);
  setupWalkinModal();

  /* ---- Step 1: header row — rename Date, drop Time, reorder ---- */
  function reorderHeader(table) {
    var headerRow = table.querySelector('thead tr');
    if (!headerRow) return;

    var cells = Array.prototype.slice.call(headerRow.children);
    if (cells.length < 8) return;

    var petTh = cells[0];
    var ownerTh = cells[1];
    var dateTh = cells[2];
    var timeTh = cells[3];
    var serviceTh = cells[4];
    var statusTh = cells[5];
    var typeTh = cells[6];
    var viewTh = cells[7];

    dateTh.textContent = 'Date & Time';
    timeTh.remove();

    headerRow.appendChild(dateTh);
    headerRow.appendChild(ownerTh);
    headerRow.appendChild(petTh);
    headerRow.appendChild(serviceTh);
    headerRow.appendChild(statusTh);
    headerRow.appendChild(typeTh);
    headerRow.appendChild(viewTh);
  }

  /* ---- Step 2: body rows — capture data, merge date/time, reorder ---- */
  function reorderRows(table) {
    var rows = table.querySelectorAll('tbody .data-table-row');

    rows.forEach(function (row) {
      var cells = Array.prototype.slice.call(row.children);
      if (cells.length < 8) return;

      var petTd = cells[0];
      var ownerTd = cells[1];
      var dateTd = cells[2];
      var timeTd = cells[3];
      var serviceTd = cells[4];
      var statusTd = cells[5];
      var typeTd = cells[6];
      var viewTd = cells[7];

      // Capture everything into data attributes BEFORE moving anything,
      // so the detail panel never depends on cell position.
      var statusBadge = statusTd.querySelector('.status-badge');

      row.dataset.pet = petTd.textContent.trim();
      row.dataset.owner = ownerTd.textContent.trim();
      row.dataset.date = dateTd.textContent.trim();
      row.dataset.time = timeTd.textContent.trim();
      row.dataset.service = serviceTd.textContent.trim();
      row.dataset.statusText = statusBadge ? statusBadge.textContent.trim() : '';
      row.dataset.statusClass = statusBadge ? statusBadge.className : 'status-badge';
      row.dataset.type = typeTd.textContent.trim();

      var viewLink = viewTd.querySelector('a.btn-link');
      row.dataset.viewHref = viewLink ? viewLink.getAttribute('href') : '#';

      // Merge time into the date cell as smaller subtext
      var dateText = row.dataset.date;
      var timeText = row.dataset.time;
      dateTd.innerHTML =
        '<span class="appt-date-main">' + escapeHtml(dateText) + '</span>' +
        '<span class="appt-time-sub">' + escapeHtml(timeText) + '</span>';

      timeTd.remove();

      // Reorder remaining cells: Date, Owner, Pet, Service, Status, Type, View
      row.appendChild(dateTd);
      row.appendChild(ownerTd);
      row.appendChild(petTd);
      row.appendChild(serviceTd);
      row.appendChild(statusTd);
      row.appendChild(typeTd);
      row.appendChild(viewTd);
    });
  }

  /* ---- Step 3: build the two-column layout + detail panel ---- */
  function buildLayout(listContainer) {
    var layout = document.createElement('div');
    layout.className = 'appointments-layout';

    var listColumn = document.createElement('div');
    listColumn.className = 'appointments-list-column';

    var panel = document.createElement('aside');
    panel.className = 'appointment-detail-panel';
    panel.id = 'appointment-detail-panel';
    panel.innerHTML = buildEmptyPanel();

    var parent = listContainer.parentNode;
    parent.insertBefore(layout, listContainer);
    listColumn.appendChild(listContainer);
    layout.appendChild(listColumn);
    layout.appendChild(panel);

    listColumn.addEventListener('click', function (e) {
      var row = e.target.closest('.data-table-row');
      if (!row) return;

      // If they clicked the "View" link itself, let it navigate normally —
      // useful as a fallback / for opening in a new tab.
      if (e.target.closest('a.btn-link')) return;

      selectRow(listColumn, row);
      loadDetailIntoPanel(panel, row.dataset.viewHref);
    });
  }

  function buildEmptyPanel() {
    return (
      '<div class="appointment-detail-empty">' +
      '<p class="appointment-detail-empty-text">Click an appointment to view its details here.</p>' +
      '</div>'
    );
  }

  function selectRow(listColumn, row) {
    var allRows = listColumn.querySelectorAll('.data-table-row');
    allRows.forEach(function (r) { r.classList.remove('data-table-row--selected'); });
    row.classList.add('data-table-row--selected');
  }

  /* Fetches the real appointment detail page and injects its .detail-layout
     (info card, Admin Notes form, Actions sidebar) straight into the panel.
     Forms inside keep working normally — they still submit and redirect
     like any regular Django form, since nothing about them is rewritten. */
  function loadDetailIntoPanel(panel, url) {
    if (!url || url === '#') return;

    panel.innerHTML =
      '<h2 class="appointment-detail-title">Appointment Details</h2>' +
      '<p class="appointment-detail-loading">Loading…</p>';

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
            '<h2 class="appointment-detail-title">Appointment Details</h2>' +
            '<p class="appointment-detail-empty-text">Unable to load details for this appointment.</p>';
          return;
        }

        panel.innerHTML = '<h2 class="appointment-detail-title">Appointment Details</h2>';
        panel.appendChild(layout);

        // Activate any hx-* attributes (e.g. the reschedule date -> slots
        // lookup) on the newly injected content, since HTMX only binds
        // automatically to elements present at its own initial page load.
        if (window.htmx) {
          window.htmx.process(layout);
        }
      })
      .catch(function () {
        panel.innerHTML =
          '<h2 class="appointment-detail-title">Appointment Details</h2>' +
          '<p class="appointment-detail-empty-text">Failed to load appointment details. Try again.</p>';
      });
  }

  function escapeHtml(str) {
    var div = document.createElement('div');
    div.textContent = str == null ? '' : str;
    return div.innerHTML;
  }

  function escapeAttr(str) {
    return String(str == null ? '' : str).replace(/"/g, '&quot;');
  }

  /* ---- Walk-in appointment modal ----
     Intercepts the "+ Walk-in Appointment" link, fetches walking_form.html
     in the background, and shows its real form in an overlay instead of
     navigating away. The form still submits normally (real POST), so a
     successful submission redirects the browser like any regular Django
     form — this modal only replaces the "getting to the form" step. */
  function setupWalkinModal() {
    var trigger = document.querySelector('.admin-page-header a.btn-primary');
    if (!trigger) return;

    var formUrl = trigger.getAttribute('href');

    trigger.addEventListener('click', function (e) {
      e.preventDefault();
      openWalkinModal(formUrl);
    });
  }

  function openWalkinModal(url) {
    var overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'walkin-modal-overlay';

    var box = document.createElement('div');
    box.className = 'modal-box';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');

    box.innerHTML =
      '<div class="modal-header">' +
      '<h2 class="modal-title">Create Walk-in Appointment</h2>' +
      '<button type="button" class="modal-close" aria-label="Close">&times;</button>' +
      '</div>' +
      '<div class="modal-body">' +
      '<p class="appointment-detail-loading">Loading…</p>' +
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

        var notice = doc.querySelector('.notice-banner');

        // walking_form.html has a duplicate <form class="booking-form">
        // opening tag (an existing bug in the template — invalid nested
        // forms). Browsers auto-close the first one on parse, so the
        // SECOND match is the real, working form with all the fields.
        var forms = doc.querySelectorAll('form.booking-form');
        var form = forms[forms.length - 1];

        if (!form) {
          body.innerHTML = '<p class="appointment-detail-empty-text">Unable to load the walk-in form.</p>';
          return;
        }

        // This form has no action="" attribute in its source template —
        // fine on its own page, but now that it's moving into a modal on
        // a DIFFERENT page, it would otherwise submit to THIS page's URL
        // instead of the real endpoint. Force it back explicitly.
        if (!form.getAttribute('action')) {
          form.setAttribute('action', url);
        }

        body.innerHTML = '';
        if (notice) body.appendChild(notice);
        body.appendChild(form);

        if (window.htmx) {
          window.htmx.process(form);
        }
      })
      .catch(function () {
        body.innerHTML = '<p class="appointment-detail-empty-text">Failed to load the form. Try again.</p>';
      });
  }
});