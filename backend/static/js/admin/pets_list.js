/* ============================================================
   pets_list.js
   For: backend/templates/admin/pets/pet_list.html
   Builds a click-to-preview side panel next to the pets table,
   fetching the real pet_detail.html content on row click — same
   approach as owners_list.js / appointments_list.js.
   ============================================================ */

document.addEventListener('DOMContentLoaded', function () {
  var tableWrapper = document.querySelector('.admin-table-wrapper');
  if (!tableWrapper) return; // empty state — nothing to build a panel for

  // Guard: don't rebuild if this has already run once on this page
  if (document.querySelector('.pets-layout')) return;

  buildLayout(tableWrapper);
  setupAddMedicalRecordModal();
  setupAddVaccinationModal();
  setupDynamicHeight();

  function buildLayout(tableWrapper) {
    var layout = document.createElement('div');
    layout.className = 'pets-layout';

    var listColumn = document.createElement('div');
    listColumn.className = 'pets-list-column';

    var panel = document.createElement('aside');
    panel.className = 'pet-detail-panel';
    panel.id = 'pet-detail-panel';
    panel.innerHTML = buildEmptyPanel();

    var parent = tableWrapper.parentNode;
    parent.insertBefore(layout, tableWrapper);
    listColumn.appendChild(tableWrapper);
    layout.appendChild(listColumn);
    layout.appendChild(panel);

    listColumn.addEventListener('click', function (e) {
      // Let any explicit link (the Owner link, or View) navigate normally
      if (e.target.closest('a')) return;

      var row = e.target.closest('.admin-table-row');
      if (!row) return;

      // Each row has TWO links sharing the same class — the Owner link
      // (admin_owner_detail) and the View link (admin_pet_detail). The
      // View link is always in the last cell, so target it specifically
      // rather than grabbing whichever link appears first in the row.
      var viewLink = row.querySelector('td:last-child a.admin-table-link');
      if (!viewLink) return;

      selectRow(listColumn, row);
      loadDetailIntoPanel(panel, viewLink.getAttribute('href'));
    });
  }

  function buildEmptyPanel() {
    return (
      '<div class="pet-detail-empty">' +
      '<p class="pet-detail-empty-text">Click a pet to view its details here.</p>' +
      '</div>'
    );
  }

  function selectRow(listColumn, row) {
    var allRows = listColumn.querySelectorAll('.admin-table-row');
    allRows.forEach(function (r) { r.classList.remove('admin-table-row--selected'); });
    row.classList.add('admin-table-row--selected');
  }

  /* Fetches the real pet_detail.html page and moves its content into the
     panel. Like owners_list.js, this page has no single wrapping class
     around everything — back-link, banners, header, and the info/medical/
     vaccination sections are all separate siblings of .admin-page — so
     this grabs .admin-page itself and moves ALL of its children in. */
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

  /* ---- Add Medical Record / Add Vaccination modals ----
     Both buttons only exist after pet_detail.html content is fetched
     into the panel, so — same as the Add Pet modal — this uses event
     delegation, matched by real URL patterns from medical/urls.py:
       Add Medical Record -> /admin/medical/pet/<pet_pk>/create/
       Add Vaccination    -> /admin/pets/<pet_pk>/vaccination/create/
     (Not to be confused with admin_vaccination_add, a different route
     used inside record_detail.html's sidebar — /vaccination/add/, not
     /vaccination/create/ — so matching /vaccination/create/ specifically
     avoids ever triggering on that one by accident.) */
  document.addEventListener('click', function (e) {
    var trigger = e.target.closest('a');
    if (!trigger) return;

    var href = trigger.getAttribute('href') || '';

    if (href.indexOf('/admin/medical/pet/') !== -1 && href.indexOf('/create/') !== -1) {
      e.preventDefault();
      openMedicalFormModal(href, 'record');
    } else if (href.indexOf('/vaccination/create/') !== -1) {
      e.preventDefault();
      openMedicalFormModal(href, 'vaccination');
    }
  });

  function openMedicalFormModal(url, type) {
    var title = type === 'record' ? 'New Medical Record' : 'Add Vaccination';

    var overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'medical-form-modal-overlay';

    var box = document.createElement('div');
    box.className = 'modal-box';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');

    box.innerHTML =
      '<div class="modal-header">' +
      '<h2 class="modal-title">' + title + '</h2>' +
      '<button type="button" class="modal-close" aria-label="Close">&times;</button>' +
      '</div>' +
      '<div class="modal-body">' +
      '<p class="pet-detail-loading">Loading…</p>' +
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
        var form = doc.querySelector('form.medical-form');

        if (!form) {
          body.innerHTML = '<p class="pet-detail-empty-text">Unable to load the form.</p>';
          return;
        }

        // Same missing-action bug as three other forms today — force it
        // back to the real endpoint since it's moving to a different page.
        if (!form.getAttribute('action')) {
          form.setAttribute('action', url);
        }

        // record_form.html shows a "Pet: X (species)" context line above
        // the form (outside it) — include it if present, for context.
        var context = doc.querySelector('.record-context');

        body.innerHTML = '';
        if (context) body.appendChild(context);
        body.appendChild(form);

        // Cancel link (vaccination_form.html has one in .form-actions) —
        // close the modal instead of navigating, same as other modals.
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
        body.innerHTML = '<p class="pet-detail-empty-text">Failed to load the form. Try again.</p>';
      });
  }

  /* ---- Dynamic height calculation ----
     Guessed pixel offsets (calc(100vh - 260px), then 320px) both missed —
     rather than guess a third number, this measures the ACTUAL rendered
     top position of the list/panel on the page and sets their height to
     exactly fill the remaining space down to the bottom of the viewport,
     minus a small margin. Recalculates on window resize too. */
  function setupDynamicHeight() {
    var listColumn = document.querySelector('.pets-list-column');
    var tableWrapperEl = document.querySelector('.admin-table-wrapper');
    var panel = document.querySelector('.pet-detail-panel');
    var bottomMargin = 24; // breathing room below the box

    function applyHeights() {
      [listColumn, tableWrapperEl, panel].forEach(function (el) {
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