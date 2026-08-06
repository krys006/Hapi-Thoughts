/* ---- Service autofill for the "Add Item" form ----
   Duplicated from receipt_detail.html's own inline <script>, since
   that script never executes for HTML fetched and parsed via
   DOMParser — only a real page load runs embedded <script> tags.
   Defined here as a true global (window.fetchServiceDetails) because
   the fetched HTML's Service <select> already has
   onchange="fetchServiceDetails(this.value)" baked into it — that
   inline attribute can only find a global function, not one scoped
   inside a DOMContentLoaded closure.
   Requires window.SERVICE_DETAILS_URL to be set via a small inline
   script in receipt_list.html (see setup instructions), since the
   endpoint URL comes from a Django {% url %} tag that only a
   template can resolve — a static .js file can't generate it. */
window.fetchServiceDetails = function (servicePk) {
  var descField = document.getElementById('id_item_description');
  var priceField = document.getElementById('id_item_unit_price');
  if (!descField || !priceField) return;

  if (!servicePk) {
    descField.value = '';
    descField.placeholder = '';
    priceField.value = '';
    priceField.placeholder = '';
    return;
  }

  if (!window.SERVICE_DETAILS_URL) {
    console.warn('SERVICE_DETAILS_URL is not defined — add it via a small inline script in receipt_list.html.');
    return;
  }

  fetch(window.SERVICE_DETAILS_URL + '?service=' + servicePk)
    .then(function (response) { return response.json(); })
    .then(function (data) {
      if (data.name) {
        descField.value = data.name;
      }
      if (data.pricing_type === 'fixed' && data.price) {
        priceField.value = data.price;
        priceField.placeholder = '';
      }
      if (data.pricing_type === 'range') {
        priceField.value = '';
        priceField.placeholder = data.placeholder;
      }
    })
    .catch(function () {
      // Fail silently — admin can still type manually
    });
};

document.addEventListener('DOMContentLoaded', function () {
  var table = document.querySelector('.data-table');
  if (!table) return; // empty state — nothing to build a panel for

  // Guard: don't rebuild if this has already run once on this page
  if (document.querySelector('.receipts-layout')) return;

  buildLayout(table);
  setupDynamicHeight();
  setupNewReceiptModal();

  function buildLayout(table) {
    var layout = document.createElement('div');
    layout.className = 'receipts-layout';

    var listColumn = document.createElement('div');
    listColumn.className = 'receipts-list-column';

    var panel = document.createElement('aside');
    panel.className = 'receipt-detail-panel';
    panel.id = 'receipt-detail-panel';
    panel.innerHTML = buildEmptyPanel();

    var parent = table.parentNode;
    parent.insertBefore(layout, table);
    listColumn.appendChild(table);
    layout.appendChild(listColumn);
    layout.appendChild(panel);

    listColumn.addEventListener('click', function (e) {
      // Let the "View" link navigate normally
      if (e.target.closest('a')) return;

      var row = e.target.closest('tbody tr');
      if (!row) return;

      var viewLink = row.querySelector('a.btn-link');
      if (!viewLink) return;

      selectRow(listColumn, row);
      loadDetailIntoPanel(panel, viewLink.getAttribute('href'));
    });
  }

  function buildEmptyPanel() {
    return (
      '<div class="receipt-detail-empty">' +
      '<p class="receipt-detail-empty-text">Click a receipt to view its full details here.</p>' +
      '</div>'
    );
  }

  function selectRow(listColumn, row) {
    var allRows = listColumn.querySelectorAll('tbody tr');
    allRows.forEach(function (r) { r.classList.remove('data-table-row--selected'); });
    row.classList.add('data-table-row--selected');
  }

  /* Fetches the real receipt_detail.html page and moves its
     .detail-layout into the panel — same technique as
     appointments_list.js / record_list.js. */
  function loadDetailIntoPanel(panel, url) {
    if (!url || url === '#') return;

    panel.innerHTML =
      '<h2 class="receipt-detail-title">Receipt Details</h2>' +
      '<p class="receipt-detail-loading">Loading…</p>';

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
            '<h2 class="receipt-detail-title">Receipt Details</h2>' +
            '<p class="receipt-detail-empty-text">Unable to load this receipt.</p>';
          return;
        }

        panel.innerHTML = '<h2 class="receipt-detail-title">Receipt Details</h2>';
        panel.appendChild(layout);

        // The fetched HTML's Service <select> already has
        // onchange="fetchServiceDetails(this.value)" baked in — since
        // window.fetchServiceDetails is defined globally above, that
        // inline attribute works correctly on its own, no manual
        // rebinding needed here.

        if (window.htmx) {
          window.htmx.process(layout);
        }
      })
      .catch(function () {
        panel.innerHTML =
          '<h2 class="receipt-detail-title">Receipt Details</h2>' +
          '<p class="receipt-detail-empty-text">Failed to load receipt. Try again.</p>';
      });
  }

  /* ---- Dynamic height — measures actual position, doesn't guess pixels ---- */
  function setupDynamicHeight() {
    var listColumn = document.querySelector('.receipts-list-column');
    var panelEl = document.querySelector('.receipt-detail-panel');
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

  /* ---- New Receipt modal ----
     "+ New Receipt" exists at page load (always visible, not per-row),
     so this binds directly rather than using event delegation. */
  function setupNewReceiptModal() {
    var trigger = document.querySelector('.admin-page-header a.btn-primary');
    if (!trigger) return;

    var formUrl = trigger.getAttribute('href');

    trigger.addEventListener('click', function (e) {
      e.preventDefault();
      openNewReceiptModal(formUrl);
    });
  }

  function openNewReceiptModal(url) {
    var overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'new-receipt-modal-overlay';

    var box = document.createElement('div');
    box.className = 'modal-box';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');

    box.innerHTML =
      '<div class="modal-header">' +
      '<h2 class="modal-title">New Receipt</h2>' +
      '<button type="button" class="modal-close" aria-label="Close">&times;</button>' +
      '</div>' +
      '<div class="modal-body">' +
      '<p class="receipt-detail-loading">Loading…</p>' +
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
          body.innerHTML = '<p class="receipt-detail-empty-text">Unable to load the form.</p>';
          return;
        }

        // Same missing-action bug hit on several other forms today.
        if (!form.getAttribute('action')) {
          form.setAttribute('action', url);
        }

        // Grab the enclosing .detail-card (keeps the "Receipt Details"
        // title for context) rather than just the bare form.
        var card = form.closest('.detail-card') || form;

        body.innerHTML = '';
        body.appendChild(card);

        var cancelLink = form.querySelector('.form-actions a.btn-secondary');
        if (cancelLink) {
          cancelLink.addEventListener('click', function (e) {
            e.preventDefault();
            closeModal();
          });
        }

        // Owner -> Pet HTMX lookup needs (re)activating on injected content.
        if (window.htmx) {
          window.htmx.process(card);
        }
      })
      .catch(function () {
        body.innerHTML = '<p class="receipt-detail-empty-text">Failed to load the form. Try again.</p>';
      });
  }
});