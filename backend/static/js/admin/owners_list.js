document.addEventListener('DOMContentLoaded', function () {
  var tableWrapper = document.querySelector('.admin-table-wrapper');
  if (!tableWrapper) return; // empty state — nothing to build a panel for

  // Guard: don't rebuild if this has already run once on this page
  if (document.querySelector('.owners-layout')) return;

  buildLayout(tableWrapper);

  function buildLayout(tableWrapper) {
    var layout = document.createElement('div');
    layout.className = 'owners-layout';

    var listColumn = document.createElement('div');
    listColumn.className = 'owners-list-column';

    var panel = document.createElement('aside');
    panel.className = 'owner-detail-panel';
    panel.id = 'owner-detail-panel';
    panel.innerHTML = buildEmptyPanel();

    var parent = tableWrapper.parentNode;
    parent.insertBefore(layout, tableWrapper);
    listColumn.appendChild(tableWrapper);
    layout.appendChild(listColumn);
    layout.appendChild(panel);

    listColumn.addEventListener('click', function (e) {
      // Let any explicit link (View, Add Pet, etc.) navigate normally
      if (e.target.closest('a')) return;

      var row = e.target.closest('.admin-table-row');
      if (!row) return;

      var viewLink = row.querySelector('a.admin-table-link');
      if (!viewLink) return;

      selectRow(listColumn, row);
      loadDetailIntoPanel(panel, viewLink.getAttribute('href'));
    });
  }

  function buildEmptyPanel() {
    return (
      '<div class="owner-detail-empty">' +
      '<p class="owner-detail-empty-text">Click a pet owner to view their details here.</p>' +
      '</div>'
    );
  }

  function selectRow(listColumn, row) {
    var allRows = listColumn.querySelectorAll('.admin-table-row');
    allRows.forEach(function (r) { r.classList.remove('admin-table-row--selected'); });
    row.classList.add('admin-table-row--selected');
  }

  /* Fetches the real owner_detail.html page and moves its content into
     the panel. Unlike appointments, this page has no single wrapping
     class around everything (back-link, archived-banner, header, and
     content are all separate siblings of .admin-page) — so instead of
     grabbing one specific class, this grabs .admin-page itself and
     moves ALL of its children into the panel, whatever they are. */
  function loadDetailIntoPanel(panel, url) {
    if (!url || url === '#') return;

    panel.innerHTML =
      '<h2 class="owner-detail-title">Owner Details</h2>' +
      '<p class="owner-detail-loading">Loading…</p>';

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
            '<h2 class="owner-detail-title">Owner Details</h2>' +
            '<p class="owner-detail-empty-text">Unable to load details for this owner.</p>';
          return;
        }

        panel.innerHTML = '';

        // Move every child of the fetched .admin-page (back-link,
        // archived-banner if present, header, content) into the panel.
        while (sourcePage.firstChild) {
          panel.appendChild(sourcePage.firstChild);
        }

        if (window.htmx) {
          window.htmx.process(panel);
        }
      })
      .catch(function () {
        panel.innerHTML =
          '<h2 class="owner-detail-title">Owner Details</h2>' +
          '<p class="owner-detail-empty-text">Failed to load owner details. Try again.</p>';
      });
  }

  /* ---- Add Pet modal ----
     "+ Add Pet" only exists in the DOM after owner_detail.html content
     is fetched into the panel (or, on the standalone owner_detail.html
     page, it's there from the start) — so this uses event delegation on
     document rather than binding to a specific element at load time,
     matched by its href pattern (/admin/pets/add/<owner_pk>/) since that
     pattern is stable regardless of where the link ends up in the DOM. */
  document.addEventListener('click', function (e) {
    var trigger = e.target.closest('a[href*="/admin/pets/add/"]');
    if (!trigger) return;

    e.preventDefault();
    openAddPetModal(trigger.getAttribute('href'));
  });

  function openAddPetModal(url) {
    var overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'add-pet-modal-overlay';

    var box = document.createElement('div');
    box.className = 'modal-box';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');

    box.innerHTML =
      '<div class="modal-header">' +
      '<h2 class="modal-title">Add Pet</h2>' +
      '<button type="button" class="modal-close" aria-label="Close">&times;</button>' +
      '</div>' +
      '<div class="modal-body">' +
      '<p class="owner-detail-loading">Loading…</p>' +
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
        var form = doc.querySelector('form.admin-form');

        if (!form) {
          body.innerHTML = '<p class="owner-detail-empty-text">Unable to load the pet form.</p>';
          return;
        }

        // The form may have no action="" attribute: fine on its own page,
        // but moved into a modal on a different page it would submit to
        // this page's URL. Force it back to the real endpoint.
        if (!form.getAttribute('action')) {
          form.setAttribute('action', url);
        }

        // pet_form.html also shows the current photo preview (edit-mode
        // only) as a sibling ABOVE the form, not inside it — include it
        // if present, same way appointments included its notice-banner.
        var photoPreview = doc.querySelector('.current-photo-wrapper');

        body.innerHTML = '';
        if (photoPreview) body.appendChild(photoPreview);
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

        // Re-activate the cascading species/breed dropdowns on the
        // freshly injected form — pet_species_breed.js binds by fixed
        // element ID, so this works the same as it does on the real page.
        var speciesSelect = form.querySelector('select.species-select');
        if (speciesSelect && window.initPetSpeciesBreedFields) {
          var speciesInputId = speciesSelect.id.replace(/_select$/, '');
          var breedSelect = form.querySelector('select.breed-select');
          var breedInputId = breedSelect ? breedSelect.id.replace(/_select$/, '') : null;
          if (breedInputId) {
            window.initPetSpeciesBreedFields(speciesInputId, breedInputId);
          }
        }
      })
      .catch(function () {
        body.innerHTML = '<p class="owner-detail-empty-text">Failed to load the form. Try again.</p>';
      });
  }
});