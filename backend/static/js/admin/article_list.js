/* ============================================================
   article_list.js
   For: backend/templates/admin/health/article_list.html
   Builds a click-to-edit side panel next to the article table,
   fetching the real article_form.html content on row click.
   ============================================================ */

document.addEventListener('DOMContentLoaded', function () {
  setupNewArticleModal();

  var table = document.querySelector('.health-article-table');
  if (!table) return; // empty state — nothing to build a panel for

  // Guard: don't rebuild if this has already run once on this page
  if (document.querySelector('.articles-layout')) return;

  buildLayout(table);
  setupDynamicHeight();

  function buildLayout(table) {
    var layout = document.createElement('div');
    layout.className = 'articles-layout';

    var listColumn = document.createElement('div');
    listColumn.className = 'articles-list-column';

    var panel = document.createElement('aside');
    panel.className = 'article-detail-panel';
    panel.id = 'article-detail-panel';
    panel.innerHTML = buildEmptyPanel();

    var parent = table.parentNode;
    parent.insertBefore(layout, table);
    listColumn.appendChild(table);
    layout.appendChild(listColumn);
    layout.appendChild(panel);

    listColumn.addEventListener('click', function (e) {
      // Let the Edit link, Publish/Unpublish form, or Delete button work normally
      if (e.target.closest('a') || e.target.closest('form') || e.target.closest('button')) return;

      var row = e.target.closest('.widget-table-row');
      if (!row) return;

      var editLink = row.querySelector('a.widget-table-link');
      if (!editLink) return;

      selectRow(listColumn, row);
      loadEditFormIntoPanel(panel, editLink.getAttribute('href'));
    });
  }

  function buildEmptyPanel() {
    return (
      '<div class="article-detail-empty">' +
      '<p class="article-detail-empty-text">Click an article to edit it here.</p>' +
      '</div>'
    );
  }

  function selectRow(listColumn, row) {
    var allRows = listColumn.querySelectorAll('.widget-table-row');
    allRows.forEach(function (r) { r.classList.remove('widget-table-row--selected'); });
    row.classList.add('widget-table-row--selected');
  }

  function resetPanel(panel) {
    panel.innerHTML = buildEmptyPanel();
  }

  /* Fetches the real article_form.html page and moves its form into
     the panel. */
  function loadEditFormIntoPanel(panel, url) {
    if (!url || url === '#') return;

    panel.innerHTML =
      '<h2 class="article-detail-title">Edit Article</h2>' +
      '<p class="article-detail-loading">Loading…</p>';

    fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (response) {
        if (!response.ok) throw new Error('Request failed');
        return response.text();
      })
      .then(function (html) {
        var parser = new DOMParser();
        var doc = parser.parseFromString(html, 'text/html');
        var form = doc.querySelector('form.health-article-form');

        if (!form) {
          panel.innerHTML =
            '<h2 class="article-detail-title">Edit Article</h2>' +
            '<p class="article-detail-empty-text">Unable to load this article.</p>';
          return;
        }

        // Same missing-action bug hit on several other forms today —
        // force it back to the real endpoint since it's moving to a
        // different page than the one it was rendered on.
        if (!form.getAttribute('action')) {
          form.setAttribute('action', url);
        }

        panel.innerHTML = '<h2 class="article-detail-title">Edit Article</h2>';
        panel.appendChild(form);

        // Cancel normally navigates back to the article list — since
        // we're already ON the list page (just with this panel open),
        // clear the panel back to empty instead of reloading the page.
        var cancelLink = form.querySelector('.form-actions a.btn-link');
        if (cancelLink) {
          cancelLink.addEventListener('click', function (e) {
            e.preventDefault();
            var allRows = document.querySelectorAll('.widget-table-row');
            allRows.forEach(function (r) { r.classList.remove('widget-table-row--selected'); });
            resetPanel(panel);
          });
        }

        if (window.htmx) {
          window.htmx.process(form);
        }
      })
      .catch(function () {
        panel.innerHTML =
          '<h2 class="article-detail-title">Edit Article</h2>' +
          '<p class="article-detail-empty-text">Failed to load article. Try again.</p>';
      });
  }

  /* ---- Dynamic height — measures actual position, doesn't guess pixels ---- */
  function setupDynamicHeight() {
    var listColumn = document.querySelector('.articles-list-column');
    var panelEl = document.querySelector('.article-detail-panel');
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

  /* ---- New Article modal ----
     "+ New Article" always exists (even on the empty state), unlike Edit
     which only appears per-row — so this binds directly rather than
     using event delegation. */
  function setupNewArticleModal() {
    var trigger = document.querySelector('.admin-page-header a.btn-primary');
    if (!trigger) return;

    var formUrl = trigger.getAttribute('href');

    trigger.addEventListener('click', function (e) {
      e.preventDefault();
      openNewArticleModal(formUrl);
    });
  }

  function openNewArticleModal(url) {
    var overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'new-article-modal-overlay';

    var box = document.createElement('div');
    box.className = 'modal-box';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');

    box.innerHTML =
      '<div class="modal-header">' +
      '<h2 class="modal-title">New Article</h2>' +
      '<button type="button" class="modal-close" aria-label="Close">&times;</button>' +
      '</div>' +
      '<div class="modal-body">' +
      '<p class="article-detail-loading">Loading…</p>' +
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
        var form = doc.querySelector('form.health-article-form');

        if (!form) {
          body.innerHTML = '<p class="article-detail-empty-text">Unable to load the form.</p>';
          return;
        }

        // Same missing-action bug hit on several other forms today.
        if (!form.getAttribute('action')) {
          form.setAttribute('action', url);
        }

        body.innerHTML = '';
        body.appendChild(form);

        var cancelLink = form.querySelector('.form-actions a.btn-link');
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
        body.innerHTML = '<p class="article-detail-empty-text">Failed to load the form. Try again.</p>';
      });
  }
});